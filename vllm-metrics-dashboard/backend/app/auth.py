"""Request identity and namespace authorization for the OAuth-protected app."""

from __future__ import annotations

from fastapi import HTTPException, Request

from .identity import Actor
from .kubernetes import kubernetes
from .settings import settings


async def request_actor(request: Request) -> Actor:
    user = request.headers.get("x-forwarded-user", "").strip()
    groups = tuple(
        group.strip()
        for header in request.headers.getlist("x-forwarded-groups")
        for group in header.split(",")
        if group.strip()
    )
    if not user and settings.local_dev_user:
        user = settings.local_dev_user
    if not user:
        raise HTTPException(status_code=401, detail="OpenShift OAuth authentication is required.")
    return Actor(user=user, groups=groups)


async def authorize_namespace(actor: Actor, namespace: str, *, verb: str = "list", resource: str = "pods") -> None:
    if namespace not in settings.allowed_namespaces:
        raise HTTPException(status_code=403, detail="Namespace is outside the configured allowlist.")
    if settings.local_dev_user:
        return
    try:
        allowed = await kubernetes.subject_access(actor, namespace, verb=verb, resource=resource)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Could not verify namespace access with OpenShift RBAC.") from exc
    if not allowed:
        raise HTTPException(status_code=403, detail="You do not have access to this namespace.")
