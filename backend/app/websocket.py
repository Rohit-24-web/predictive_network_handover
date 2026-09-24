"""M7: WebSocket live telemetry -> prediction -> decision.

A transport, not a second pipeline. Every frame is handled by the SAME components
the HTTP path uses:

    normalize_android_sample (M5) -> TelemetryStore (M5)
      -> run_telemetry_decision (shared) -> HandoverOrchestrator (M4)
      -> Predictor (M1) + DecisionEngine (M3)

STATE
-----
Both the DecisionEngine (dwell, cooldown, poor_streak, interruption) and the
TelemetryStore are stateful and keyed by session_id. A WebSocket connection binds
to ONE session_id for its lifetime and reuses those existing stores, so state is
never reset per message and never shared between sessions.

Note the guards below use `is None` rather than truthiness: TelemetryStore and
SessionStore both define __len__, so an empty store is falsy.

SCALING
-------
Session state is in-process. Multiple uvicorn workers would each hold their own
stores, so a multi-worker deployment needs sticky routing by session_id or a shared
store. Single worker is correct for M7; see README.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from app.telemetry import TelemetryError, TelemetryStore, normalize_android_sample
from app.telemetry_flow import FlowError, decision_payload, run_telemetry_decision

logger = logging.getLogger("handover.ws")

# --- protocol -------------------------------------------------------------
MSG_TELEMETRY = "telemetry"
MSG_PING = "ping"
CLIENT_MESSAGE_TYPES = {MSG_TELEMETRY, MSG_PING}

ERR_INVALID_JSON = "invalid_json"
ERR_INVALID_TYPE = "invalid_message_type"
ERR_VALIDATION = "validation_error"
ERR_FLOW = "flow_error"
ERR_INTERNAL = "internal_error"


class ConnectionManager:
    """Tracks live sockets so disconnects are always cleaned up."""

    def __init__(self) -> None:
        self._active: Dict[str, Set[WebSocket]] = {}

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._active.setdefault(session_id, set()).add(ws)

    def disconnect(self, session_id: str, ws: WebSocket) -> None:
        sockets = self._active.get(session_id)
        if sockets is None:
            return
        sockets.discard(ws)
        if not sockets:
            self._active.pop(session_id, None)

    def connection_count(self, session_id: Optional[str] = None) -> int:
        if session_id is None:
            return sum(len(v) for v in self._active.values())
        return len(self._active.get(session_id, ()))


def _error(code: str, message: str) -> Dict[str, Any]:
    """Structured error. Never carries a stack trace to the client."""
    return {"type": "error", "code": code, "message": message}


def handle_telemetry_message(state: Dict[str, Any], session_id: str,
                             message: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Process one telemetry frame and return the frames to send back.

    Pure and synchronous so it can be unit-tested without a socket. Returns an ack
    followed by either a decision or a status frame -- never a fabricated prediction.
    """
    store: TelemetryStore = state.get("telemetry_store")
    if store is None:
        return [_error(ERR_INTERNAL, "service not ready")]

    raw_sample = message.get("sample")
    if not isinstance(raw_sample, dict):
        return [_error(ERR_VALIDATION, "message is missing a 'sample' object")]

    mode = message.get("mode", "predictive")
    if mode not in ("reactive", "predictive"):
        return [_error(ERR_VALIDATION, f"unknown mode {mode!r}")]

    allow_estimate = bool(message.get("allow_sinr_estimate", False))
    merge_into = message.get("merge_real_radio_into", "LEO-1")

    # --- M5 normalisation and buffering, unchanged ---
    try:
        sample, warnings = normalize_android_sample(
            raw_sample, allow_sinr_estimate=allow_estimate
        )
        session = store.ingest(session_id, sample)
    except TelemetryError as e:
        return [_error(ERR_VALIDATION, str(e))]
    except (TypeError, ValueError) as e:
        return [_error(ERR_VALIDATION, f"malformed telemetry sample: {e}")]

    frames: list[Dict[str, Any]] = [{
        "type": "telemetry_ack",
        "session_id": session_id,
        "samples_received": session.received,
        "buffer_size": session.buffer_size,
        "required_window_steps": store.window_size,
        "window_full": session.window_full,
        "prediction_ready": session.prediction_ready(),
        "sinr_source": sample.sinr_source,
        "warnings": warnings,
    }]

    # Before the window is full we acknowledge and stop. No prediction is invented.
    if not session.prediction_ready():
        frames.append({
            "type": "status",
            "session_id": session_id,
            "prediction_ready": False,
            "buffer_size": session.buffer_size,
            "required_window_steps": store.window_size,
            "reason": ("waiting for a full telemetry window"
                       if not session.window_full
                       else "at least one buffered sample has no SINR"),
        })
        return frames

    try:
        result = run_telemetry_decision(state, session_id, mode, merge_into)
    except FlowError as e:
        frames.append(_error(e.code, e.detail))
        return frames
    except Exception:
        logger.exception("WebSocket decision failed for session %s", session_id)
        frames.append(_error(ERR_INTERNAL, "internal decision error"))
        return frames

    frames.append({
        "type": "decision",
        "session_id": session_id,
        "simulation_step": result.simulation_step,
        "merged_path": result.merged_path,
        "field_provenance": result.provenance,
        "notes": result.notes,
        "decision": decision_payload(result),
    })
    return frames


async def telemetry_websocket(ws: WebSocket, session_id: str,
                              state: Dict[str, Any],
                              manager: ConnectionManager) -> None:
    """Serve one WebSocket connection bound to a single session_id."""
    await manager.connect(session_id, ws)
    logger.info("WebSocket connected: session=%s", session_id)
    try:
        store: TelemetryStore = state.get("telemetry_store")
        await ws.send_json({
            "type": "connected",
            "session_id": session_id,
            "required_window_steps": (store.window_size if store is not None else None),
            "protocol": {
                "client": ["telemetry", "ping"],
                "server": ["connected", "telemetry_ack", "status", "decision", "pong", "error"],
            },
            "note": ("Send REAL cellular telemetry. LEO/MEO/GEO candidate conditions "
                     "are simulated."),
        })

        while True:
            raw = await ws.receive_text()

            try:
                message = json.loads(raw)
            except json.JSONDecodeError as e:
                await ws.send_json(_error(ERR_INVALID_JSON, f"could not parse JSON: {e.msg}"))
                continue

            if not isinstance(message, dict):
                await ws.send_json(_error(ERR_INVALID_JSON, "message must be a JSON object"))
                continue

            msg_type = message.get("type")
            if msg_type not in CLIENT_MESSAGE_TYPES:
                await ws.send_json(_error(
                    ERR_INVALID_TYPE,
                    f"unsupported message type {msg_type!r}; "
                    f"expected one of {sorted(CLIENT_MESSAGE_TYPES)}",
                ))
                continue

            if msg_type == MSG_PING:
                await ws.send_json({"type": "pong", "session_id": session_id})
                continue

            try:
                for frame in handle_telemetry_message(state, session_id, message):
                    await ws.send_json(frame)
            except Exception:
                # A bad frame must never kill the connection or leak a traceback.
                logger.exception("Unhandled error on session %s", session_id)
                await ws.send_json(_error(ERR_INTERNAL, "internal server error"))

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: session=%s", session_id)
    except Exception:
        logger.exception("WebSocket terminated abnormally: session=%s", session_id)
    finally:
        manager.disconnect(session_id, ws)


# ---------------------------------------------------------------------------
# Observer sockets (read-only)
# ---------------------------------------------------------------------------
async def observer_websocket(ws: WebSocket, session_id: str,
                             state: Dict[str, Any], hub) -> None:
    """Stream a producer session's frames to a read-only subscriber.

    This is how the React dashboard watches the Vivo: the phone keeps posting to
    HTTP /telemetry, and every resulting frame is fanned out here. The observer
    never sends telemetry, so it cannot influence the buffer or the decision
    engine — it only watches.
    """
    await ws.accept()
    queue = hub.subscribe(session_id)
    store: TelemetryStore = state.get("telemetry_store")
    session = store.get(session_id) if store is not None else None
    logger.info("Observer connected: session=%s", session_id)

    try:
        await ws.send_json({
            "type": "connected",
            "role": "observer",
            "session_id": session_id,
            "required_window_steps": (store.window_size if store is not None else None),
            "producer_present": session is not None,
            "buffer_size": session.buffer_size if session else 0,
            "samples_received": session.received if session else 0,
            "protocol": {"client": [], "server": ["connected", "telemetry_ack", "status",
                                                  "decision", "error"]},
            "note": ("Read-only view of a producer session. Cellular telemetry is REAL; "
                     "LEO/MEO/GEO candidate conditions are simulated."),
        })

        # Two concurrent waits: frames to forward, and the peer closing. Without the
        # receive task a disconnect would only surface on the next publish, which may
        # be never for an idle session.
        pump = asyncio.create_task(queue.get())
        peer = asyncio.create_task(ws.receive_text())
        try:
            while True:
                done, _ = await asyncio.wait({pump, peer},
                                             return_when=asyncio.FIRST_COMPLETED)
                if peer in done:
                    peer.result()           # raises WebSocketDisconnect on close
                    # Observers are read-only; anything they send is ignored politely.
                    await ws.send_json(_error(
                        ERR_INVALID_TYPE,
                        "this is a read-only observer socket; connect to /ws/{session_id} to send telemetry",
                    ))
                    peer = asyncio.create_task(ws.receive_text())
                if pump in done:
                    await ws.send_json(pump.result())
                    pump = asyncio.create_task(queue.get())
        finally:
            for task in (pump, peer):
                task.cancel()
    except WebSocketDisconnect:
        logger.info("Observer disconnected: session=%s", session_id)
    except Exception:
        logger.exception("Observer socket terminated abnormally: session=%s", session_id)
    finally:
        hub.unsubscribe(session_id, queue)
