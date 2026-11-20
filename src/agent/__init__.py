"""Timestral paper-trading agent.

Pipeline: data -> forecast (Timestral) -> decide (Mistral LLM) -> guardrails
-> execute (Alpaca) -> audit.
"""
