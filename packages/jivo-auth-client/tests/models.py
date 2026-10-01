from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models


class Member(AbstractBaseUser):
    """
    A consuming application's own user model after migrating to Jivo Auth:
    it keeps its username and gains an `auth_id` column for the Jivo ID.
    """

    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(blank=True)
    auth_id = models.UUIDField(null=True, blank=True, unique=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = BaseUserManager()

    USERNAME_FIELD = "username"
    EMAIL_FIELD = "email"
