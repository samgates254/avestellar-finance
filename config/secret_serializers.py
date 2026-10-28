"""Serializer helpers that keep provider credentials private and durable.

Two problems are addressed here, because both are ways an integration secret can
be lost or exposed through the ordinary configuration API:

1. Read-back: a credential stored in a plain database column must never be
   returned by a serializer, so secret fields are forced to ``write_only``.
2. Accidental wipe: a PATCH with ``"api_key": ""`` (or a form that submits every
   field it knows about) must not erase a working credential. Clearing requires
   the explicit ``clear_secrets=true`` flag.

Field-level encryption at rest is *not* provided here; see
``docs/staging_deployment.md`` for the documented limitation.
"""

from rest_framework import serializers

SECRET_CLEAR_FLAG = "clear_secrets"


class SecretFieldProtectionMixin:
    """Mixin for ModelSerializers that expose provider credentials.

    Subclasses declare:

    - ``secret_fields``: credential fields, forced write-only.
    - ``secret_fields_required_on_create``: the subset that has no model default
      and must therefore be supplied when the record is first created.
    """

    secret_fields: tuple[str, ...] = ()
    secret_fields_required_on_create: tuple[str, ...] = ()
    clear_flag_field = SECRET_CLEAR_FLAG

    def get_extra_kwargs(self):
        extra_kwargs = super().get_extra_kwargs()
        for field in self.secret_fields:
            kwargs = extra_kwargs.setdefault(field, {})
            kwargs["write_only"] = True
            kwargs["required"] = False
            kwargs.setdefault("allow_blank", True)
        return extra_kwargs

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is None:
            missing = [
                field
                for field in self.secret_fields_required_on_create
                if not str(attrs.get(field) or "").strip()
            ]
            if missing:
                raise serializers.ValidationError(
                    {field: "This field is required." for field in missing}
                )
            return attrs

        # Update: a blank submission means "leave it alone" unless the caller
        # explicitly asks to clear stored credentials, in which case every secret
        # field is cleared (submitted or not).
        if bool(self.initial_data.get(self.clear_flag_field)):
            for field in self.secret_fields:
                if field not in attrs:
                    attrs[field] = ""
            return attrs

        for field in self.secret_fields:
            if field in attrs and not str(attrs.get(field) or "").strip():
                attrs.pop(field)
        return attrs
