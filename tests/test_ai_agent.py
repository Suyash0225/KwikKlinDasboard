        # build_ai_reply routes tools before composing a customer response.
        # Keep tool-router calls separate from the compose response.
        if calls in {1, 3}:
            return {"tool_calls": []}
        if calls == 2:
            return {
                "reply": "Ji, apna naam bata dijiye.",
                "intent": "NEW_ORDER", "language": "hi",
                "action": "CREATE_LEAD", "action_reason": "new enquiry",
                "escalate": False, "escalation_reason": "", "admin_note": "",
                "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
            }