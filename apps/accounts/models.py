"""The project's user model.

Email is the login identifier, so ``username`` is removed and
``USERNAME_FIELD`` is pointed at ``email``. This is the standard Django recipe
("Using a custom user model when starting a project") and must be decided before
the first migration is ever applied - which is why the migration history was
reset rather than amended.
"""

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class Role(models.TextChoices):
    ANALYST = "analyst", "Analyst"
    ADMINISTRATOR = "administrator", "Administrator"


class UserManager(BaseUserManager):
    """Creates users from an email instead of a username."""

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", Role.ADMINISTRATOR)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("A superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("A superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField("email address", unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.ANALYST)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self):
        return self.email

    @property
    def is_administrator(self):
        return self.role == Role.ADMINISTRATOR
