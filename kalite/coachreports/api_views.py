import datetime, re, json, sys, logging
from annoying.decorators import render_to
from annoying.functions import get_object_or_None
from functools import partial
from collections import OrderedDict

from django.utils import simplejson
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseForbidden, HttpResponseNotFound, HttpResponseServerError
from django.shortcuts import render_to_response, get_object_or_404, redirect, get_list_or_404
from django.template import RequestContext
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.utils import simplejson

from main.models import VideoLog, ExerciseLog, VideoFile
from securesync.models import Facility, FacilityUser,FacilityGroup, DeviceZone, Device
from utils.decorators import require_admin
from utils.topics import slug_key, title_key
from securesync.views import facility_required
#from shared.views import group_report_context
from coachreports.forms import DataForm
from main import topicdata
from config.models import Settings
from utils.topic_tools import get_topic_by_path


# Global variable of all the known stats, their internal and external names, 
#    and their "datatype" (which is a value that Google Visualizations uses)
stats_dict = [
    { "key": "pct_mastery",        "name": "% Mastery",          "type": "number" },
    { "key": "effort",             "name": "Effort",             "type": "number" },
    { "key": "ex:attempts",        "name": "Average attempts",   "type": "number" },
    { "key": "ex:streak_progress", "name": "Average streak",     "type": "number" },
    { "key": "ex:points",          "name": "Exercise points",    "type": "number" },
    { "key": "ex:completion_timestamp", "name": "Time completed","type": "datetime" },
]


class JsonResponse(HttpResponse):
    def __init__(self, content, *args, **kwargs):
        if not isinstance(content, str) and not isinstance(content, unicode):
            content = simplejson.dumps(content, ensure_ascii=False)
        super(JsonResponse, self).__init__(content, content_type='application/json', *args, **kwargs)


class StatusException(Exception):
    def __init__(self, message, status_code):
        super(StatusException, self).__init__(message)
        self.args = (status_code,)
        self.status_code = status_code


def get_data_form(request, *args, **kwargs):
    """Request objects get priority over keyword args"""
    assert not args, "all non-request args should be keyword args"
    
    # Pull the form parameters out of the request or 
    data = dict()
    for field in ["facility_id", "group_id", "user_id", "xaxis", "yaxis"]:
        # Default to empty string, as it makes template handling cleaner later.
        data[field] = request.REQUEST.get(field, kwargs.get(field, ""))
    data["topic_path"] = request.REQUEST.getlist("topic_path")
    form = DataForm(data = data)
    
    # Filling in data for superusers
    if not "facility_user" in request.session:
        if request.user.is_superuser:
            if not (form.data["facility_id"] or form.data["group_id"] or form.data["user_id"]):
                facility = kwargs.get("facility")
                group = None if FacilityGroup.objects.all().count() !=1 else FacilityGroup.objects.all()[0]
            
                if group and not form.data["group_id"]:
                    form.data["group_id"] = group.id
                if facility and not form.data["facility_id"]:
                    form.data["facility_id"] = facility.id
                    

    # Filling in data for FacilityUsers
    else:
        user = request.session["facility_user"]
        group = None if not user else user.group
        # Facility can come from user, facility 
        facility = kwargs.get("facility") if not user else user.facility

        # Fill in default query data
        if not (form.data["facility_id"] or form.data["group_id"] or form.data["user_id"]):
        
            # Defaults:
            #   Students: only themselves
            #   Teachers: if nothing is specified, then show their group
        
            if request.is_admin:
                if group:
                    form.data["group_id"] = group.id
                elif facility:
                    form.data["facility_id"] = facility.id
                else: # not a meaningful default, but responds efficiently (no data)
                    form.data["user_id"] = user.id
            else:
                form.data["user_id"] = user.id    
        
        ######
        # Authenticate
        if group and form.data["group_id"] and group.id != form.data["group_id"]: # can't go outside group
            # We could also redirect
            HttpResponseForbidden("You cannot choose a group outside of your group.")
        elif facility and form.data["facility_id"] and facility.id != form.data["facility_id"]:
            # We could also redirect
            HttpResponseForbidden("You cannot choose a facility outside of your own facility.")
        elif not request.is_admin:
            if not form.data["user_id"]:
                # We could also redirect
                HttpResponseForbidden("You cannot choose facility/group-wide data.")
            elif user and form.data["user_id"] and user.id != form.data["user_id"]:
                # We could also redirect
                HttpResponseForbidden("You cannot choose a user outside of yourself.")

    # Fill in backwards: a user implies a group
    if form.data.get("user_id") and not form.data.get("group_id"):
         user = get_object_or_404(FacilityUser, id=form.data["user_id"])
         form.data["group_id"] = getattr(user.group, "id")

    if form.data.get("group_id") and not form.data.get("facility_id"):
         group = get_object_or_404(FacilityGroup, id=form.data["group_id"])
         form.data["facility_id"] = getattr(group.facility, "id")
    
    return form    


def compute_data(types, who, where):
    """
    Compute the data in "types" for each user in "who", for the topics selected by "where"
    
    who: list of users
    where: topic_path
    types can include:
        pct_mastery
        effort
        attempts
    """
    
    # None indicates that the data hasn't been queried yet.
    #   We'll query it on demand, for efficiency
    topics = None
    exercises = None
    videos = None

    # Initialize an empty dictionary of data, video logs, exercise logs, for each user
    data     = OrderedDict(zip([w.id for w in who], [dict() for i in range(len(who))])) #maintain the order of the users
    vid_logs = dict(zip([w.id for w in who], [None   for i in range(len(who))]))
    ex_logs  = dict(zip([w.id for w in who], [None   for i in range(len(who))]))

    # Set up queries (but don't run them), so we have really easy aliases.
    #   Only do them if they haven't been done yet (tell this by passing in a value to the lambda function)      
    # Topics: topics.
    # Exercises: names (ids for ExerciseLog objects)
    # Videos: youtube_id (ids for VideoLog objects)
    search_fun      = partial(lambda t,p: t["path"].startswith(p), p=tuple(where))
    query_topics    = partial(lambda t,sf: t if t is not None else [t           for t   in filter(sf, topicdata.NODE_CACHE['Topic'].values())],sf=search_fun)
    query_exercises = partial(lambda e,sf: e if e is not None else [ex["name"]  for ex  in filter(sf, topicdata.NODE_CACHE['Exercise'].values())],sf=search_fun)
    query_videos    = partial(lambda v,sf: v if v is not None else [vid["youtube_id"] for vid in filter(sf, topicdata.NODE_CACHE['Video'].values())],sf=search_fun)

    # Exercise log and video log dictionary (key: user)
    query_exlogs    = lambda u,ex,el:  el if el is not None else ExerciseLog.objects.filter(user=u, exercise_id__in=ex).order_by("completion_timestamp")
    query_vidlogs   = lambda u,vid,vl: vl if vl is not None else VideoLog.objects.filter(user=u, youtube_id__in=vid).order_by("completion_timestamp")
    
    # No users, don't bother.
    if len(who)>0:
        for type in (types if not hasattr(types,"lower") else [types]): # convert list from string, if necessary
            if type in data[data.keys()[0]]: # if the first user has it, then all do; no need to calc again.
                continue
            
            #
            # These are summary stats: you only get one per user
            #
            if type == "pct_mastery":
                exercises = query_exercises(exercises)
            
                # Efficient query out, spread out to dict
                # ExerciseLog.filter(user__in=who, exercise_id__in=exercises).order_by("user.id")
                for user in data.keys():
                    ex_logs[user] = query_exlogs(user, exercises, ex_logs[user]) 
                    data[user][type] = 0 if not ex_logs[user] else 100.*sum([el.complete for el in ex_logs[user]])/float(len(exercises))
                    
            elif type == "effort":
                if "ex:attempts" in data[data.keys()[0]] and "vid:total_seconds_watched" in data[data.keys()[0]]:
                    # exercises and videos would be initialized already
                    for user in data.keys():
                        avg_attempts = 0 if len(exercises)==0 else sum(data[user]["ex:attempts"].values())/float(len(exercises))
                        avg_seconds_watched = 0 if len(videos)==0 else sum(data[user]["vid:total_seconds_watched"].values())/float(len(videos))
                        data[user][type] = avg_attempts/10. + avg_seconds_watched/750.
                else:
                    types += ["ex:attempts", "vid:total_seconds_watched", "effort"]
            

            #
            # These are detail stats: you get many per user
            #
            
            # Just querying out data directly: Video
            elif type.startswith("vid:") and type[4:] in [f.name for f in VideoLog._meta.fields]:
                videos = query_videos(videos)
                for user in data.keys():
                    vid_logs[user] = query_vidlogs(user, videos, vid_logs[user])
                    data[user][type] = OrderedDict([(v.youtube_id, getattr(v, type[4:])) for v in vid_logs[user]])
        
            # Just querying out data directly: Exercise
            elif type.startswith("ex:") and type[3:] in [f.name for f in ExerciseLog._meta.fields]:
                exercises = query_exercises(exercises)
                for user in data.keys():
                    ex_logs[user] = query_exlogs(user, exercises, ex_logs[user])
                    data[user][type] = OrderedDict([(el.exercise_id, getattr(el,type[3:])) for el in ex_logs[user]])
            
            # Unknown requested quantity     
            else:
                raise Exception("Unknown type: '%s' not in %s" % (type, str([f.name for f in ExerciseLog._meta.fields])))

    return {
        "data": data,
        "topics": topics,
        "exercises": exercises,
        "videos": videos,
    }


    
@csrf_exempt
def api_data(request, xaxis="", yaxis=""):
#    if request.method != "POST":
#        return HttpResponseForbidden("%s request not allowed." % request.method)
    
    # Get the request form
    form = get_data_form(request, xaxis=xaxis, yaxis=yaxis)#(data=request.REQUEST)

    # Query out the data: who?
    if form.data.get("user_id"):
        facility = []
        groups = []
        users = [get_object_or_404(FacilityUser, id=form.data.get("user_id"))]
    elif form.data.get("group_id"):
        facility = []
        groups = [get_object_or_404(FacilityGroup, id=form.data.get("group_id"))]
        users = FacilityUser.objects.filter(group=form.data.get("group_id"), is_teacher=False).order_by("last_name", "first_name")
    elif form.data.get("facility_id"):
        facility = get_object_or_404(Facility, id=form.data.get("facility_id"))
        groups = FacilityGroup.objects.filter(facility__in=[form.data.get("facility_id")])
        users = FacilityUser.objects.filter(group__in=groups, is_teacher=False).order_by("last_name", "first_name")
    else:
        return HttpResponseNotFound("Did not specify facility, group, nor user.")

    # Query out the data: where?
    if not form.data.get("topic_path"):
        return HttpResponseServerError("Must specify a topic path")

    # Query out the data: what?
    try:
        computed_data = compute_data(types=[form.data.get("xaxis"), form.data.get("yaxis")], who=users, where=form.data.get("topic_path"))
        json_data = {
            "data": computed_data["data"],
            "exercises": computed_data["exercises"],
            "videos": computed_data["videos"],
            "users": dict( zip( [u.id for u in users],
                                ["%s, %s" % (u.last_name, u.first_name) for u in users]
                         )),
            "groups":  dict( zip( [g.id for g in groups],
                                 dict(zip(["id", "name"], [(g.id, g.name) for g in groups])),
                          )),
            "facility": None if not facility else {
                "name": facility.name,
                "id": facility.id,
            }
        }
    
        # Now we have data, stream it back with a handler for date-times
        dthandler = lambda obj: obj.isoformat() if isinstance(obj, datetime.datetime) else None
        return HttpResponse(content=json.dumps(json_data, default=dthandler), content_type="application/json")

    except Exception as e:
        return HttpResponseServerError(str(e))
    
    
@csrf_exempt
def api_friendly_names(request):
    """api_data returns raw data with identifiers.  This endpoint is a generic endpoint
    for mapping IDs to friendly names."""
    
    
    return None


def convert_topic_tree(node, level=0):
    if node["kind"] == "Topic":
        if "Exercise" not in node["contains"]:
            return None
        children = []
        for child_node in node["children"]:
            child = convert_topic_tree(child_node, level=level+1)
            if child:
                children.append(child)
        return {
            "title": node["title"],
            "tooltip": re.sub(r'<[^>]*?>', '', node["description"] or ""),
            "isFolder": True,
            "key": node["path"],
            "children": children,
            "expand": level < 1,
        }
    return None

def get_topic_tree(request, topic_path="/topics/math/"):
    return JsonResponse(convert_topic_tree(get_topic_by_path(topic_path)));
    
