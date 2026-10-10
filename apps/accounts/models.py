"""Accounts holds no models of its own.

Roles are expressed with ``django.contrib.auth.models.Group`` so that the
project keeps the stock ``User`` model and Django's own admin, permission
machinery, and password hashing.
"""
