"""Version-one suggestion contract. Evaluation is read-only; run is explicit."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime

from homeassistant.components.http import HomeAssistantView

from .auth_api import authenticate_request, get_engine, json_body
from .const import DOMAIN
from .energy_runtime import energy_lockout_applies
from .registry_api import async_execute_registry_scene
from .storage import StorageError
from .suggestions import MAX_RULES, SuggestionError, evaluate, validate_rule


def runtime_for(hass):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


class _SuggestionView(HomeAssistantView):
    requires_auth = False

    def __init__(self, hass):
        self.hass = hass

    def json(self, result, status=200):
        # Keep the machine code and the common CasaSmart error envelope.
        # Tablet transports deliberately do not trust a bare HTTP status.
        if status >= 400 and isinstance(result, dict) and "error" in result:
            result = {"message": str(result["error"]).replace("_", " "), **result}
        return super().json(result, status)

    async def handle(self, request, permission, operation):
        claims, error = authenticate_request(self.hass, request, permission)
        if error is not None:
            return error
        # Management replaces the whole bounded document; scoped administrators
        # must not read or overwrite hidden rules in that document.
        if permission == "suggestions.manage" and claims.get("rooms") is not None:
            return self.json({"error": "unrestricted_admin_required"}, 403)
        runtime = runtime_for(self.hass)
        service = getattr(runtime, "suggestions", None)
        if service is None:
            return self.json({"error": "suggestions_unavailable"}, 503)
        try:
            return await operation(service, claims)
        except SuggestionError as err:
            return self.json({"error": err.code}, err.status)
        except (StorageError, sqlite3.Error):
            return self.json({"error": "suggestion_storage_unavailable"}, 503)

    def member(self, claims):
        auth = get_engine(self.hass)
        return auth.member_id_for(claims["sub"]) if auth else claims["sub"]

    async def body(self, request, allowed):
        body = await json_body(request)
        if not isinstance(body, dict) or set(body) - allowed:
            raise SuggestionError("invalid_request")
        return body


class CasaSmartSuggestionsView(_SuggestionView):
    url = f"/api/{DOMAIN}/now/suggestions"
    name = f"api:{DOMAIN}:now:suggestions"

    async def get(self, request):
        async def operation(service, claims):
            return self.json(
                await service.payload(self.member(claims), claims.get("rooms"))
            )

        return await self.handle(request, "devices.read", operation)


class CasaSmartSuggestionRulesView(_SuggestionView):
    url = f"/api/{DOMAIN}/now/suggestions/rules"
    name = f"api:{DOMAIN}:now:suggestions:rules"

    async def get(self, request):
        async def operation(service, claims):
            data = await self.hass.async_add_executor_job(service.store.snapshot)
            return self.json(
                {"version": 1, "revision": data["revision"], "rules": data["rules"]}
            )

        return await self.handle(request, "suggestions.manage", operation)

    async def put(self, request):
        async def operation(service, claims):
            body = await self.body(request, {"expected_revision", "rules"})
            raw = body.get("rules")
            if not isinstance(raw, list) or len(raw) > MAX_RULES:
                raise SuggestionError("invalid_rules")
            rules = [validate_rule(r) for r in raw]
            context = await service.context()
            for rule in rules:
                self.references(service, context, rule, claims)
            result = await self.hass.async_add_executor_job(
                service.store.replace_rules, body.get("expected_revision"), rules
            )
            await service.refresh()
            return self.json(result)

        return await self.handle(request, "suggestions.manage", operation)

    @staticmethod
    def references(service, context, rule, claims):
        scene = context[1].get(rule["scene_id"])
        if not scene or not scene.get("entities"):
            raise SuggestionError("invalid_scene_reference")
        ids = {i["entity_id"] for i in scene["entities"]} | {
            c["entity_id"] for c in rule["conditions"]
        }
        if not all(
            service.visible(claims.get("rooms"))(eid)
            and service.hass.states.get(eid) is not None
            for eid in ids
        ):
            raise SuggestionError("invalid_entity_reference")
        return scene


class CasaSmartSuggestionPreviewView(_SuggestionView):
    url = f"/api/{DOMAIN}/now/suggestions/preview"
    name = f"api:{DOMAIN}:now:suggestions:preview"

    async def post(self, request):
        async def operation(service, claims):
            body = await self.body(request, {"rule"})
            rule = validate_rule(body.get("rule"))
            context = await service.context()
            scene = CasaSmartSuggestionRulesView.references(
                service, context, rule, claims
            )
            ids = {i["entity_id"] for i in scene["entities"]} | {
                c["entity_id"] for c in rule["conditions"]
            }
            states = {eid: self.hass.states.get(eid) for eid in ids}
            suggestion, reason = evaluate(
                rule,
                scene,
                states,
                context[3],
                context[4],
                service.sunset,
                service.visible(claims.get("rooms")),
            )
            return self.json(
                {
                    "version": 1,
                    "eligible": suggestion is not None,
                    "reason": reason,
                    "suggestion": suggestion,
                }
            )

        return await self.handle(request, "suggestions.manage", operation)


class CasaSmartSuggestionActionView(_SuggestionView):
    url = f"/api/{DOMAIN}/now/suggestions/actions"
    name = f"api:{DOMAIN}:now:suggestions:actions"

    async def post(self, request):
        # Dismiss/snooze require read access; Run is checked again with control
        # permission immediately before acquiring a durable execution claim.
        async def operation(service, claims):
            body = await self.body(request, {"action", "occurrence_id"})
            action, occurrence = body.get("action"), body.get("occurrence_id")
            if (
                action not in ("dismiss", "snooze", "run")
                or not isinstance(occurrence, str)
                or len(occurrence) != 64
            ):
                raise SuggestionError("invalid_action")
            if action == "run":
                claims, error = authenticate_request(
                    self.hass, request, "devices.control"
                )
                if error is not None:
                    return error
            member, scope = self.member(claims), claims.get("rooms")
            context = await service.context()
            candidate = next(
                (
                    s
                    for _, s, _ in service.candidates(
                        context, scope, policy_checks=False
                    )
                    if s and s["occurrence_id"] == occurrence
                ),
                None,
            )
            if candidate is None:
                raise SuggestionError("occurrence_expired_or_ineligible", 409)
            receipt = context[0]["executions"].get(occurrence)
            if action == "run" and receipt:
                return self.json(
                    receipt, 202 if receipt["status"] == "executing" else 200
                )
            previous = context[0]["suppressions"].get(
                service.store.suppression_key(member, occurrence)
            )
            if (
                action in ("dismiss", "snooze")
                and previous
                and previous["action"] == action
                and datetime.fromisoformat(previous["until"]) > context[3]
            ):
                return self.json(
                    {
                        "status": "snoozed" if action == "snooze" else "dismissed",
                        "until": previous["until"],
                    }
                )
            selected = service.payload_from(context, member, scope)["suggestion"]
            if selected is None or selected["occurrence_id"] != occurrence:
                raise SuggestionError("occurrence_expired_or_ineligible", 409)
            if action != "run":
                result = await self.hass.async_add_executor_job(
                    service.store.suppress, member, candidate, action, context[3]
                )
                await service.refresh()
                return self.json(result)
            scene = context[1][candidate["scene_id"]]
            self.energy_check(claims, scene)
            claimed, receipt = await self.hass.async_add_executor_job(
                service.store.claim, candidate, context[3]
            )
            if not claimed:
                return self.json(
                    receipt, 202 if receipt["status"] == "executing" else 200
                )
            dispatched = False
            try:
                # Executor scheduling is an async gap: re-read rules, scene,
                # conditions, time and token privileges before the first action.
                await service.refresh()
                fresh = await service.context()
                fresh_claims, error = authenticate_request(
                    self.hass, request, "devices.control"
                )
                if error is not None:
                    await self.hass.async_add_executor_job(
                        service.store.abort_before_dispatch, occurrence
                    )
                    return error
                fresh_candidate = next(
                    (
                        s
                        for _, s, _ in service.candidates(
                            fresh, fresh_claims.get("rooms")
                        )
                        if s and s["occurrence_id"] == occurrence
                    ),
                    None,
                )
                if fresh_candidate is None:
                    raise SuggestionError("occurrence_expired_or_ineligible", 409)
                suppressed = fresh[0]["suppressions"].get(
                    service.store.suppression_key(self.member(fresh_claims), occurrence)
                )
                if (
                    suppressed
                    and datetime.fromisoformat(suppressed["until"]) > fresh[3]
                ):
                    raise SuggestionError("occurrence_expired_or_ineligible", 409)
                scene = fresh[1][candidate["scene_id"]]
                self.energy_check(fresh_claims, scene)
                dispatched = True
                result = await async_execute_registry_scene(self.hass, scene)
                receipt = await self.hass.async_add_executor_job(
                    service.store.finish, occurrence, result
                )
            except BaseException:
                await asyncio.shield(
                    self.hass.async_add_executor_job(
                        service.store.unknown
                        if dispatched
                        else service.store.abort_before_dispatch,
                        occurrence,
                    )
                )
                raise
            finally:
                await service.refresh()
            return self.json(receipt)

        return await self.handle(request, "devices.read", operation)

    def energy_check(self, claims, scene):
        energy = getattr(runtime_for(self.hass), "energy", None)
        if energy is not None and energy_lockout_applies(energy, claims):
            raise SuggestionError("energy_lockout", 403)
        if (
            energy is not None
            and energy.active_level is not None
            and not scene.get("works_during_energy_saving", False)
        ):
            raise SuggestionError("scene_skipped_energy_saving", 409)
