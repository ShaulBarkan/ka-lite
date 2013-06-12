from annoying.functions import get_object_or_None

from django.http import HttpResponse, HttpResponseNotFound, HttpResponseRedirect, HttpResponseForbidden, HttpResponseServerError
from django.shortcuts import render_to_response, get_object_or_404, redirect, get_list_or_404
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, HttpResponseNotFound
from django.utils.safestring import mark_safe
from django.contrib.auth.decorators import login_required
from django.utils.translation import ugettext as _

import settings
from securesync.models import Device, DeviceZone, Zone, Facility
from central.models import Organization
from config.models import Settings

def require_admin(handler):
    def wrapper_fn(request, *args, **kwargs):
        if (settings.CENTRAL_SERVER and request.user.is_authenticated()) or getattr(request, "is_admin", False):
            return handler(request, *args, **kwargs)
        # Translators: Please ignore the html tags e.g. "Please login as one below, or <a href='%s'>go to home.</a>" is simply "Please login as one below, or go to home."
        messages.error(request, mark_safe(_("To view the page you were trying to view, you need to be logged in as a teacher or an admin. Please login as one below, or <a href='%s'>go to home.</a>") % reverse("homepage")))
        return HttpResponseRedirect(reverse("login") + "?next=" + request.path)
    return wrapper_fn
    


def central_server_only(handler):
    def wrapper_fn(*args, **kwargs):
        if not settings.CENTRAL_SERVER:
            return HttpResponseNotFound("This path is only available on the central server.")
        return handler(*args, **kwargs)
    return wrapper_fn


def distributed_server_only(handler):
    def wrapper_fn(*args, **kwargs):
        if settings.CENTRAL_SERVER:
            return HttpResponseNotFound(_("This path is only available on distributed servers."))
        return handler(*args, **kwargs)
    return wrapper_fn
    


def facility_from_request(handler):
    def wrapper_fn(request, *args, **kwargs):
        if kwargs.get("facility_id",None):
            facility = get_object_or_None(pk=facility_id)
        elif "facility" in request.GET:
            facility = get_object_or_None(Facility, pk=request.GET["facility"])
            if "set_default" in request.GET and request.is_admin and facility:
                Settings.set("default_facility", facility.id)
        elif "facility_user" in request.session:
            facility = request.session["facility_user"].facility
        elif Facility.objects.count() == 1:
            facility = Facility.objects.all()[0]
        else:
            facility = get_object_or_None(Facility, pk=Settings.get("default_facility"))
        return handler(request, *args, facility=facility, **kwargs)
    return wrapper_fn


def facility_required(handler):
    @facility_from_request
    def inner_fn(request, facility, *args, **kwargs):
        if facility:
            return handler(request, facility, *args, **kwargs)

        if Facility.objects.count() == 0:
            if request.is_admin:
                messages.error(request, _("To continue, you must first add a facility (e.g. for your school). ") \
                    + _("Please use the form below to add a facility."))
            else:
                messages.error(request,
                    _("You must first have the administrator of this server log in below to add a facility."))
            return HttpResponseRedirect(reverse("add_facility"))
        else:
            return facility_selection(request)
    
    return inner_fn