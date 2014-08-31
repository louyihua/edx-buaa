# -*- coding: UTF-8 -*-
import logging
import json

from django.conf import settings
from django_future.csrf import ensure_csrf_cookie
from django.core.serializers.json import DjangoJSONEncoder
from django.contrib.auth import logout
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from microsite_configuration import microsite
from courseware.courses import course_image_url, get_course_about_section,get_courses,get_course_with_access,sort_by_announcement
from student.views import get_course_enrollment_pairs, login_user, change_enrollment
from student.models import CourseEnrollment
from util.json_request import JsonResponse
from util.cache import cache_if_anonymous
from django.http import Http404, HttpResponse
from opaque_keys.edx.locations import SlashSeparatedCourseKey
from django.contrib.auth.models import User
from courseware.access import has_access
from courseware.views import registered_for_course,save_child_position,get_current_child
from courseware.model_data import FieldDataCache
from courseware.module_render import get_module_for_descriptor,toc_for_course
from xmodule.modulestore.django import modulestore
from xmodule.x_module import STUDENT_VIEW
from xmodule.x_module import XModule
from xmodule.video_module.video_module import VideoDescriptor
from user_api.models import UserPreference
from lang_pref import LANGUAGE_KEY


log = logging.getLogger("edx.courseware")

@ensure_csrf_cookie
def mobile_api(request, apiname = ""):
    if apiname == "init":
        return JsonResponse({ "success": True })
    elif apiname == "get_course_enrollment":
        return get_course_enrollment(request)
    elif apiname == "login":
        return login(request)
    elif apiname == "courses":
        return courses(request)
    elif apiname == "course_about":
        return course_about(request, request.POST.get('course_id', None))
    elif apiname == "course_courseware":
        return course_courseware(request, request.POST.get('course_id', None), request.POST.get('chapter', None), request.POST.get('section', None), request.POST.get('position', None))
    elif apiname == "logout":
        return logout_user(request)
    elif apiname == "course_enroll":
        return course_enroll(request)
    else:
        return JsonResponse({}, status = 403)


def login(request):
    response = login_user(request, "")
    context = json.loads(response.content)
    if context.get("success", False):
        context.update({
            "user_name": request.user.username,
            "user_full_name": request.user.profile.name,
            "language_code": UserPreference.get_preference(request.user, LANGUAGE_KEY),
        })
        response.content = json.dumps(context, cls = DjangoJSONEncoder, indent = 2, ensure_ascii = False)   
    return response


def logout_user(request):
    logout(request)
    response = JsonResponse({"success": True})
    response.delete_cookie(settings.EDXMKTG_COOKIE_NAME,
        path='/', domain=settings.SESSION_COOKIE_DOMAIN,)
    return response


def get_course_enrollment(request):
    if not request.user.is_authenticated():
        return JsonResponse({ "status": False })

    # for microsites, we want to filter and only show enrollments for courses
    # within the microsites 'ORG'
    course_org_filter = microsite.get_value('course_org_filter')

    # Let's filter out any courses in an "org" that has been declared to be
    # in a Microsite
    org_filter_out_set = microsite.get_all_orgs()

    # remove our current Microsite from the "filter out" list, if applicable
    if course_org_filter:
        org_filter_out_set.remove(course_org_filter)

    # Build our (course, enrollment) list for the user, but ignore any courses
    # that no longer exist (because the course IDs have changed). Still, we don't
    # delete those enrollments, because it could have been a data push snafu.
    course_enrollment_pairs = list(get_course_enrollment_pairs(request.user, course_org_filter, org_filter_out_set))

    enrollment_list = []
    for course, enrollment in course_enrollment_pairs:
        item = {
            "course_image_url": course_image_url(course),
            "course_id": course.id.to_deprecated_string(),
            "display_organization": get_course_about_section(course, 'university'),
            "display_number": course.display_number_with_default,
            "display_name": course.display_name_with_default,
            "course_start": course.start,
            "course_end": course.end,
            "enrollment_start": course.enrollment_start,
            "enrollment_end": course.enrollment_end,
            "advertised_start": course.advertised_start,
            "enrollment_date": enrollment.created,
            "active": enrollment.is_active,
        }
    enrollment_list.append(item)
    
    return JsonResponse({ "status": True, "enrollment": enrollment_list })


def courses(request):
    """
    Render "find courses" page.  The course selection work is done in courseware.courses.
    """
    courses = sort_by_announcement(get_courses(request.user, request.META.get('HTTP_HOST')))
    course_list = []
    for course in courses:
        course_item = {
            "display_number": course.display_number_with_default,
            "course_title": get_course_about_section(course, 'title'),
            "course_description": get_course_about_section(course, 'short_description'),
            "display_organization": get_course_about_section(course, 'university'),
            "course_image_url": course_image_url(course),
            "course_start": course.start,
            "course_id": course.id.to_deprecated_string(),
        }
        course_list.append(course_item)

    return JsonResponse(course_list)


def course_about(request, course_id):
    user = request.user
    course = get_course_with_access(user, 'see_exists', SlashSeparatedCourseKey.from_deprecated_string(course_id))

    return JsonResponse({
        'display_number': course.display_number_with_default,
        'display_name': get_course_about_section(course, "title"),
        'display_organization': get_course_about_section(course, "university"),
        'about': get_course_about_section(course, "overview"),
        'registered': CourseEnrollment.is_enrolled(user, course.id) if user is not None and user.is_authenticated() else False,
        'is_full': CourseEnrollment.is_course_full(course) # see if we have already filled up all allowed enrollments
    })

def course_courseware(request, course_id, chapter = None, section = None, position = None):
    if not request.user.is_authenticated():
        return JsonResponse({ "status": False })

    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)
    user = User.objects.prefetch_related("groups").get(id = request.user.id)
    request.user = user  # keep just one instance of User
    course = get_course_with_access(user, 'load', course_key, depth = 2)
    if not registered_for_course(course, user):
        # TODO (vshnayder): do course instructors need to be registered to see
        # course?
        return JsonResponse({ 'status': False })

    try:
        field_data_cache = FieldDataCache.cache_for_descriptor_descendents(course_key, user, course, depth = 2)
        course_module = get_module_for_descriptor(user, request, course, field_data_cache, course_key)
        has_content = course.has_children_at_depth(2)
        if not has_content:
            # Show empty courseware for a course with no units
            return JsonResponse({ 'status': False })

        if position is not None:
            try:
                int(position)
            except ValueError:
                return JsonResponse({ 'status': False })

        def get_units(chapter, section, position = None):
            chapter_descriptor = course.get_child_by(lambda m: m.location.name == chapter)
            section_descriptor = chapter_descriptor.get_child_by(lambda m: m.location.name == section)
            # cdodge: this looks silly, but let's refetch the section_descriptor with depth=None
            # which will prefetch the children more efficiently than doing a recursive load
            section_descriptor = modulestore().get_item(section_descriptor.location, depth = None)
            section_field_data_cache = FieldDataCache.cache_for_descriptor_descendents(course_key, user, section_descriptor, depth = None)
            section_module = get_module_for_descriptor(request.user, request, section_descriptor, section_field_data_cache, course_key, position)

            if section_module is not None:
                units = []
                for unit in section_module.get_display_items():
                    verticals = []
                    for vertical in unit.get_display_items():
                        if isinstance(vertical, VideoDescriptor):
                            subtitles = vertical.transcripts.copy()
                            if vertical.sub != "":
                                subtitles.update({ 'en': sub })
                            verticals.append({
                                'name': vertical.display_name,
                                'video_sources': vertical.html5_sources,
                                'subtitles': subtitles,
                                'type': 'video'
                            })
                        else:
                            verticals.append({
                                'name': vertical.display_name,
                                'type': 'other'
                            })
                    units.append({
                        'name': unit.display_name,
                        'verticals': verticals
                    })
                return units
            else:
                return None

        if chapter is None or section is None:
            context = {
                'course_id': course.id.to_deprecated_string(),
                'sections': toc_for_course(user, request, course, chapter, section, field_data_cache),
                'course_title': course.display_name_with_default,
                'status': True
            }
            for chapter in context.get('sections', []):
                for section in chapter.get('sections', []):
                    section.update({ 'units': get_units(chapter['url_name'], section['url_name'])})
        else:
            units = get_units(chapter, section, position)
            context = {
                'units': units,
                'status': True if units is not None else False
            }

    except Exception as e:
        # In production, don't want to let a 500 out for any reason
        if settings.DEBUG:
            raise
        else:
            log.exception(u"Error in index view: user={user}, course={course}, chapter={chapter}"
                u" section={section} position={position}".format(user=user,
                    course=course,
                    chapter=chapter,
                    section=section,
                    position=position))
            context = { 'status': False }

    return JsonResponse(context)


def course_enroll(request):
    response = change_enrollment(request)
    if isinstance(response, (HttpResponseBadRequest, HttpResponseForbidden)):
        return JsonResponse({ 'status': False, 'reason': response.content })
    else:
        return JsonResponse({ 'status': True })
