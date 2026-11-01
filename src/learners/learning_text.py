"""Text extraction shared by native learning paths.

Learning must use the sender's own top-level text.  Media descriptions,
quotes, forwards and other message components are context for chat, but are
not reliable evidence of a reusable expression or jargon term.
"""

from src.common.data_models.message_component_data_model import MessageSequence, TextComponent


def extract_learning_text(sequence: MessageSequence) -> str:
    """Return only top-level text components from a message sequence."""

    return " ".join(
        component.text
        for component in sequence.components
        if isinstance(component, TextComponent)
    ).strip()
