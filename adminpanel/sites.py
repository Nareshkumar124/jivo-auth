from django.conf import settings
from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import path, reverse

from .dashboard import build_dashboard
from .docs import build_docs


# Sidebar sections, in order: (title, [(app_label, model name, label, icon)]).
# Models the user may not see are left out; so is a section left empty.
NAV_SECTIONS = [
    (
        "People",
        [
            ("users", "user", "Users", "users"),
            ("adminpanel", "staffrole", "Staff roles", "shield"),
        ],
    ),
    (
        "Access",
        [
            ("applications", "application", "Applications", "grid"),
            ("authentication", "usersession", "Sessions", "monitor"),
        ],
    ),
    (
        "Security",
        [
            ("audit", "auditevent", "Audit log", "activity"),
        ],
    ),
]


def build_nav(request, available_apps):
    models = {
        (app["app_label"], model["object_name"].lower()): model
        for app in available_apps
        for model in app["models"]
    }

    placed = set()
    sections = []

    for title, entries in NAV_SECTIONS:
        items = []

        for app_label, model_name, label, icon in entries:
            model = models.get((app_label, model_name))

            if model is None or not model.get("admin_url"):
                continue

            placed.add((app_label, model_name))
            items.append(
                {
                    "label": label,
                    "icon": icon,
                    "url": model["admin_url"],
                    "current": request.path.startswith(model["admin_url"]),
                }
            )

        if items:
            sections.append({"title": title, "items": items})

    # Anything else registered later still gets a place.
    others = [
        {
            "label": model["name"],
            "icon": "folder",
            "url": model["admin_url"],
            "current": request.path.startswith(model["admin_url"]),
        }
        for key, model in models.items()
        if key not in placed and model.get("admin_url")
    ]

    if others:
        sections.append({"title": "Other", "items": others})

    # For every staff member, whatever their permissions.
    docs_url = reverse("admin:docs")
    sections.append(
        {
            "title": "Resources",
            "items": [
                {
                    "label": "Docs",
                    "icon": "book",
                    "url": docs_url,
                    "current": request.path.startswith(docs_url),
                },
            ],
        }
    )

    return sections


class JivoAdminSite(admin.AdminSite):

    site_header = "Jivo Auth"
    site_title = "Jivo Auth admin"
    index_title = "Dashboard"

    # "View site" leads to the API reference: the service has no site.
    site_url = "/api/docs/"

    def get_urls(self):
        return [
            path("docs/", self.admin_view(self.docs), name="docs"),
            *super().get_urls(),
        ]

    def docs(self, request):
        return TemplateResponse(
            request,
            "adminpanel/docs/drf.html",
            {
                **self.each_context(request),
                "title": "Integrate a Django REST Framework API",
                **build_docs(request),
            },
        )

    def each_context(self, request):
        context = super().each_context(request)

        context["nav_sections"] = build_nav(request, context["available_apps"])
        context["dashboard_url"] = reverse("admin:index", current_app=self.name)
        context["environment"] = "Development" if settings.DEBUG else "Production"

        return context

    def index(self, request, extra_context=None):
        return super().index(
            request,
            {
                **(extra_context or {}),
                "dashboard": build_dashboard(request),
            },
        )
