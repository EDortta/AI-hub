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
    # GPT-5.5/5.6 (medido ao vivo 2026-07-17): a UI removeu o rótulo "Thinking" e
    # nomeia o esforço de raciocínio como níveis no seletor do composer.
    "Medium",
    "High",
    "Ask ChatGPT\nMedium",       # innerText do form com a pill "Medium"
    "Ask ChatGPT\nHigh",
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
    "Instant",           # nível padrão do GPT-5.5/5.6 = SEM raciocínio (verificado ao vivo)
    "Ask ChatGPT\nInstant",
])
def test_default_model_is_not_flagged(label):
    """Um falso positivo aqui bloqueia TODA geração — pior que o bug original."""
    from image_generator import is_reasoning_mode
    assert is_reasoning_mode(label) is False


def test_word_boundary_avoids_false_positives():
    """'rethinking' contém 'thinking' mas não é um modo — a checagem é por palavra.

    'medium'/'high' são palavras comuns; a pill do composer só mostra a palavra do
    modo e a leitura ocorre com o composer vazio (antes de _fill_and_send), então o
    prompt do usuário nunca é lido aqui — mas a checagem continua sendo por palavra.
    """
    from image_generator import is_reasoning_mode
    assert is_reasoning_mode("rethinking the design") is False
    assert is_reasoning_mode("unthinking") is False
    assert is_reasoning_mode("highlight the logo") is False   # 'high' colado em 'light'
    assert is_reasoning_mode("premium medium-rare") is True   # 'medium' isolado casa


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


# --- Issue 009 (corrida de hidratação): o chip de modo aparece ~3s DEPOIS do input
# box; ler cedo devolvia vazio e o guard não disparava mesmo em modo de raciocínio.
# settled_composer_mode faz poll até o chip hidratar antes de julgar.

class _ScriptedPage:
    """Page cujo form innerText evolui a cada leitura, simulando a hidratação."""

    def __init__(self, sequence):
        self._seq = list(sequence)
        self.reads = 0

    def evaluate(self, _js):
        i = min(self.reads, len(self._seq) - 1)
        self.reads += 1
        return self._seq[i]


def _fake_clock():
    """Relógio determinístico: avança 0.25s a cada consulta (sem tempo real)."""
    t = {"now": 0.0}

    def now():
        t["now"] += 0.25
        return t["now"]

    return now


def test_settled_mode_waits_for_hydration_then_detects_reasoning():
    """Vazio, vazio, e então 'Medium' — deve esperar e devolver o rótulo real."""
    from image_generator import settled_composer_mode, is_reasoning_mode

    page = _ScriptedPage(["", "", "", "Medium"])
    label = settled_composer_mode(
        page, timeout_s=100, poll_s=0, clock=_fake_clock(), sleep=lambda _s: None
    )
    assert label == "Medium"
    assert is_reasoning_mode(label) is True


def test_settled_mode_returns_instant_without_false_positive():
    """Instant hidrata: é modo conhecido, devolve cedo e NÃO é raciocínio."""
    from image_generator import settled_composer_mode, is_reasoning_mode

    page = _ScriptedPage(["", "Instant"])
    label = settled_composer_mode(
        page, timeout_s=100, poll_s=0, clock=_fake_clock(), sleep=lambda _s: None
    )
    assert label == "Instant"
    assert is_reasoning_mode(label) is False


def test_settled_mode_fails_open_when_no_chip_ever_appears():
    """GPT sem seletor de modo: nunca hidrata → timeout → segue a geração."""
    from image_generator import settled_composer_mode, is_reasoning_mode

    page = _ScriptedPage([""])
    label = settled_composer_mode(
        page, timeout_s=1, poll_s=0, clock=_fake_clock(), sleep=lambda _s: None
    )
    assert is_reasoning_mode(label) is False  # None/'' → não bloqueia


def test_settled_mode_survives_unreadable_page():
    """evaluate levantando não derruba a geração (fail open)."""
    from image_generator import settled_composer_mode

    class _Broken:
        def evaluate(self, _js):
            raise RuntimeError("UI mudou")

    label = settled_composer_mode(
        _Broken(), timeout_s=1, poll_s=0, clock=_fake_clock(), sleep=lambda _s: None
    )
    assert label is None
