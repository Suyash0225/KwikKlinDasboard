    # Keep the configured provider/model so provider-specific tests can select
    # their own backend. All HTTP entry points below remain blocked by mocks.
    monkeypatch.setattr(llm_module, "FALLBACK_PROVIDER", llm_module.PROVIDER)
    monkeypatch.setattr(llm_module, "SECONDARY_FALLBACK_PROVIDER", llm_module.PROVIDER)