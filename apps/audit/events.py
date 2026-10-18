"""The audit vocabulary: which actions are recorded, and what may never be.

An audit event is a **decision or a privileged action**, not a heartbeat. The
list below is deliberately short: every entry is something an auditor would ask
"who did that, to what, and when", and every entry names an actor and a target.

The other half of the vocabulary is the prohibition. Audit rows are kept
indefinitely, so anything written into ``metadata`` is kept indefinitely too --
which means a *checker*, not a careful author, has to be what keeps credentials
out. :func:`sanitize_metadata` is that check.
"""

from __future__ import annotations


#: The recorded actions, namespaced ``<app>.<event>``. The namespace means a new
#: event cannot collide with an existing one (``ingestion.job_created`` can never
#: be mistaken for ``accounts.job_created``) and a reader can tell where it came
#: from without a lookup.
#:
#: These are plain constants rather than model ``choices`` on purpose: the column
#: is a free ``CharField``, so adding an event is a code change and not a
#: migration. An audit table that needed a schema change to learn a new verb
#: would be extended less often than it should be.
class AuditAction:
    JOB_CREATED = "ingestion.job_created"
    JOB_QUEUED = "ingestion.job_queued"
    UPLOAD_LOADED = "ingestion.upload_loaded"
    JOB_CANCELLED = "ingestion.job_cancelled"
    ROLE_CHANGED = "accounts.role_changed"


#: Keys that must never reach ``AuditEvent.metadata``, matched case-insensitively
#: at every level of a nested payload.
#:
#: A deny-list rather than an allow-list: callers add a field far more often than
#: they add a secret, and an allow-list would silently drop every new field until
#: someone remembered to permit it. A deny-list that misses one key still catches
#: the whole familiar family -- ``password`` catches ``new_password``,
#: ``token`` catches ``refresh_token``.
NEVER_LOGGED_KEYS = frozenset(
    {
        "password",
        "password1",
        "password2",
        "new_password",
        "old_password",
        "token",
        "access",
        "refresh",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "auth",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "private_key",
        "signing_key",
        "session",
        "sessionid",
        "session_key",
        "cookie",
        "csrf",
        "csrfmiddlewaretoken",
    }
)

#: The longest string kept. A truncated hint still names the shape of a payload,
#: where a full body could carry a credential the deny-list did not happen to
#: name.
MAX_STRING_LENGTH = 500

#: How deep :func:`sanitize_metadata` will walk before it stops recursing. A
#: bound, not a policy: it turns a pathological or self-referential payload into a
#: placeholder instead of a recursion error inside an audit write.
MAX_DEPTH = 6


def sanitize_metadata(value, *, depth: int = 0):
    """Return a JSON-safe copy of ``value`` with forbidden keys removed.

    Contract
    --------
    in : any value a caller wants to attach to an event -- normally a dict of
         small primitives, but nothing stops a caller passing a model or a
         ``Decimal``.
    out: the same structure with every :data:`NEVER_LOGGED_KEYS` key dropped at
         every level, every string truncated to :data:`MAX_STRING_LENGTH`, and
         every non-JSON value coerced to its string form.

    Business purpose: the one gate between "the caller remembered not to log the
    password" and "the row is in the table forever". Because it also coerces
    non-serializable values, a caller that passes a stray object fails *down* to a
    string rather than *up* into a ``TypeError`` that would have taken the audit
    write -- and possibly the operation it was describing -- down with it.
    """
    if depth > MAX_DEPTH:
        return "<truncated>"

    if isinstance(value, dict):
        return {
            str(key): sanitize_metadata(item, depth=depth + 1)
            for key, item in value.items()
            if str(key).lower() not in NEVER_LOGGED_KEYS
        }

    # ``str`` is a sequence too, so it is handled before the generic sequence
    # branch and never exploded into a list of characters.
    if isinstance(value, str):
        return value[:MAX_STRING_LENGTH]

    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_metadata(item, depth=depth + 1) for item in value]

    # ``bool`` before ``int``: ``True`` is an ``int`` in Python, and it should
    # stay a boolean rather than becoming ``1``.
    if value is None or isinstance(value, (bool, int, float)):
        return value

    return str(value)[:MAX_STRING_LENGTH]
