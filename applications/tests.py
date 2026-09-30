import uuid
from io import StringIO

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import UserSession
from users.models import User

from .models import Application, hash_api_key


PASSWORD = "Str0ng!Pass"


class ApplicationTestCase(APITestCase):

    def setUp(self):
        cache.clear()

        self.oms = Application.objects.create(slug="oms", name="OMS")
        self.api_key = self.oms.set_new_api_key()

        self.alice = User.objects.create_user(
            email="alice@jivo.in",
            password=PASSWORD,
            first_name="Alice",
            is_verified=True,
        )
        self.oms.users.add(self.alice)


class ApiKeyTests(ApplicationTestCase):

    def test_only_the_key_hash_is_stored(self):
        self.oms.refresh_from_db()

        self.assertTrue(self.api_key.startswith("jivo_"))
        self.assertEqual(self.oms.api_key_hash, hash_api_key(self.api_key))
        self.assertEqual(self.oms.api_key_prefix, self.api_key[:12])

    def test_from_api_key(self):
        self.assertEqual(Application.from_api_key(self.api_key), self.oms)
        self.assertIsNone(Application.from_api_key("jivo_wrong"))
        self.assertIsNone(Application.from_api_key(""))

    def test_inactive_application_key_does_not_work(self):
        Application.objects.update(is_active=False)

        self.assertIsNone(Application.from_api_key(self.api_key))

    def test_command_rotates_key(self):
        stdout = StringIO()

        call_command("app_api_key", "oms", stdout=stdout, stderr=StringIO())

        new_key = stdout.getvalue().strip()

        self.assertNotEqual(new_key, self.api_key)
        self.assertEqual(Application.from_api_key(new_key), self.oms)
        self.assertIsNone(Application.from_api_key(self.api_key))

    def test_command_creates_application(self):
        stdout = StringIO()

        call_command(
            "app_api_key",
            "ecom",
            "--create",
            "Jivo Ecom",
            stdout=stdout,
            stderr=StringIO(),
        )

        ecom = Application.objects.get(slug="ecom")
        self.assertEqual(ecom.name, "Jivo Ecom")
        self.assertEqual(Application.from_api_key(stdout.getvalue().strip()), ecom)

    def test_command_refuses_unknown_application(self):
        with self.assertRaises(CommandError):
            call_command("app_api_key", "nope", stdout=StringIO(), stderr=StringIO())

    def test_command_validates_new_slug(self):
        with self.assertRaises(ValidationError):
            call_command(
                "app_api_key",
                "Not A Slug",
                "--create",
                "Bad",
                stdout=StringIO(),
                stderr=StringIO(),
            )


class ApplicationUsersTests(ApplicationTestCase):

    def setUp(self):
        super().setUp()

        self.bob = User.objects.create_user(email="bob@jivo.in")
        self.oms.users.add(self.bob)

        self.carol = User.objects.create_user(email="carol@jivo.in")

    def list_users(self, key=None, **params):
        return self.client.get(
            reverse("application-users"),
            params,
            HTTP_X_JIVO_APP_KEY=self.api_key if key is None else key,
        )

    def test_requires_api_key(self):
        response = self.client.get(reverse("application-users"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rejects_invalid_api_key(self):
        response = self.list_users(key="jivo_wrong")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rejects_user_access_token(self):
        access = self.client.post(
            reverse("login"),
            {"email": "alice@jivo.in", "password": PASSWORD},
        ).json()["access"]

        response = self.client.get(
            reverse("application-users"),
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_lists_only_users_with_access(self):
        response = self.list_users()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [user["email"] for user in response.json()],
            ["alice@jivo.in", "bob@jivo.in"],
        )
        self.assertEqual(
            response.json()[0],
            {
                "id": str(self.alice.id),
                "email": "alice@jivo.in",
                "first_name": "Alice",
                "last_name": "",
                "employee_code": "",
                "is_active": True,
            },
        )

    def test_filters_by_id(self):
        response = self.client.get(
            f"{reverse('application-users')}"
            f"?id={self.bob.id}&id={self.carol.id}&id={uuid.uuid4()}",
            HTTP_X_JIVO_APP_KEY=self.api_key,
        )

        self.assertEqual(
            [user["email"] for user in response.json()],
            ["bob@jivo.in"],
        )

    def test_rejects_invalid_id(self):
        response = self.list_users(id="not-a-uuid")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("id", response.json())

    def test_rejects_too_many_ids(self):
        query = "&".join(f"id={uuid.uuid4()}" for _ in range(101))

        response = self.client.get(
            f"{reverse('application-users')}?{query}",
            HTTP_X_JIVO_APP_KEY=self.api_key,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ForwardedClientTests(ApplicationTestCase):

    def login(self, **extra):
        return self.client.post(
            reverse("login"),
            {"email": "alice@jivo.in", "password": PASSWORD},
            REMOTE_ADDR="10.0.0.5",
            **extra,
        )

    def test_login_records_ip_forwarded_by_application(self):
        self.login(
            HTTP_X_JIVO_APP_KEY=self.api_key,
            HTTP_X_JIVO_CLIENT_IP="203.0.113.7",
        )

        self.assertEqual(UserSession.objects.get().ip_address, "203.0.113.7")

    def test_forwarded_ip_ignored_without_valid_api_key(self):
        self.login(
            HTTP_X_JIVO_APP_KEY="jivo_wrong",
            HTTP_X_JIVO_CLIENT_IP="203.0.113.7",
        )

        self.assertEqual(UserSession.objects.get().ip_address, "10.0.0.5")

    def test_malformed_forwarded_ip_is_ignored(self):
        self.login(
            HTTP_X_JIVO_APP_KEY=self.api_key,
            HTTP_X_JIVO_CLIENT_IP="not-an-ip",
        )

        self.assertEqual(UserSession.objects.get().ip_address, "10.0.0.5")

    def test_login_rate_limit_is_per_end_user(self):
        # settings: "login": "5/min"
        for _ in range(5):
            self.login(
                HTTP_X_JIVO_APP_KEY=self.api_key,
                HTTP_X_JIVO_CLIENT_IP="203.0.113.7",
            )

        blocked = self.login(
            HTTP_X_JIVO_APP_KEY=self.api_key,
            HTTP_X_JIVO_CLIENT_IP="203.0.113.7",
        )
        other_user = self.login(
            HTTP_X_JIVO_APP_KEY=self.api_key,
            HTTP_X_JIVO_CLIENT_IP="198.51.100.3",
        )

        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(other_user.status_code, status.HTTP_200_OK)


class ApplicationUsersEmployeeCodeTests(ApplicationTestCase):

    def test_lookup_includes_employee_code(self):
        User.objects.filter(pk=self.alice.pk).update(employee_code="JIVO1")

        response = self.client.get(
            reverse("application-users"),
            HTTP_X_JIVO_APP_KEY=self.api_key,
        )

        self.assertEqual(response.json()[0]["employee_code"], "JIVO1")
