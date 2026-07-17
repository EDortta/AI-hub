"""Issue 002 — lost GPT context must be detected, not polled through.

The symptom was `page_url` reverting from the GPT to the bare `chatgpt.com/`
home: the send never created a turn, and the waiter then watched the wrong page
for ~600s. `is_chatgpt_home` is the judgement that turns that into a fast, named
failure, so it is tested directly.
"""
from __future__ import annotations

import pytest

from image_generator import is_chatgpt_home


@pytest.mark.parametrize("url", [
    "https://chatgpt.com/",
    "https://chatgpt.com",
    "https://chatgpt.com/?model=auto",
    "https://chatgpt.com/#anchor",
    "https://chat.openai.com/",
    "https://chat.openai.com",
])
def test_home_urls_are_recognised(url):
    assert is_chatgpt_home(url) is True


@pytest.mark.parametrize("url", [
    "https://chatgpt.com/g/g-pmuQfob8d-image-generator",
    "https://chatgpt.com/g/g-pmuQfob8d-image-generator/c/abc-123",
    "https://chatgpt.com/c/67f0-conversation",
    "https://chat.openai.com/g/g-something",
])
def test_gpt_and_conversation_urls_are_not_home(url):
    assert is_chatgpt_home(url) is False


@pytest.mark.parametrize("url", ["", None])
def test_missing_url_is_not_treated_as_home(url):
    """An unknown URL must not be reported as 'context lost' — that error names a
    specific cause and would send the operator down the wrong path."""
    assert is_chatgpt_home(url) is False


def test_unrelated_hosts_are_not_home():
    assert is_chatgpt_home("https://example.com/") is False
    assert is_chatgpt_home("https://notchatgpt.com/") is False


def test_lookalike_host_is_not_home():
    """Substring matching would misfire here; the check is on the whole base URL."""
    assert is_chatgpt_home("https://evil-chatgpt.com/") is False
    assert is_chatgpt_home("https://chatgpt.com.evil.net/") is False


# --- Issue 009: modo de raciocínio não gera imagem -------------------------
#
# Medido na UI real (2026-07-17): com "Thinking", 11 min sem imagem e timeout;
# com o modelo padrão, 30s e imagem. O chip de modo só existe no composer quando
# um modo NÃO-padrão está escolhido — por isso "sem rótulo" significa "padrão".

@pytest.mark.parametrize("label", [
    "Thinking",
    "thinking",
    "⊙ Thinking ⌄",
    "Ask ChatGPT\nThinking",     # innerText do form inteiro, como lido em produção
    "Reasoning",
    "Raciocínio",
])
def test_reasoning_modes_are_detected(label):
    from image_generator import is_reasoning_mode
    assert is_reasoning_mode(label) is True


@pytest.mark.parametrize("label", [
    None,          # sem chip = modelo padrão = o caso bom (verificado ao vivo)
    "",
    "Ask ChatGPT",
    "GPT-5",
    "Auto",
])
def test_default_model_is_not_flagged(label):
    """Um falso positivo aqui bloqueia TODA geração — pior que o bug original."""
    from image_generator import is_reasoning_mode
    assert is_reasoning_mode(label) is False


def test_word_boundary_avoids_false_positives():
    """'rethinking' contém 'thinking' mas não é um modo — a checagem é por palavra."""
    from image_generator import is_reasoning_mode
    assert is_reasoning_mode("rethinking the design") is False
    assert is_reasoning_mode("unthinking") is False


def test_unreadable_composer_fails_open(monkeypatch):
    """Se a UI mudar e não dermos conta de ler, a geração SEGUE.

    Isto é diagnóstico, não controle de segurança: bloquear por não conseguir ler
    trocaria um problema raro por um permanente (design-standards §6).
    """
    from image_generator import _composer_mode_label

    class _Page:
        def evaluate(self, _js):
            raise RuntimeError("UI mudou")

    assert _composer_mode_label(_Page()) is None
    from image_generator import is_reasoning_mode
    assert is_reasoning_mode(_composer_mode_label(_Page())) is False
