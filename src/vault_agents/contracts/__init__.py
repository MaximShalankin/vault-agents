"""Контракты координации: tickets + inbox (ticket-012).

Спецификации — данные пакета (inbox-proto-1.json, tickets-v1.json):
единственный источник правил. Валидатор читает спеки и не содержит
собственных словарей — CLI и тонкие внешние хуки зовут одно и то же.
"""
from .validator import (
    Report,
    load_spec,
    slugify_from,
    validate_file,
    validate_inbox_message,
    validate_ticket,
)

__all__ = [
    "Report",
    "load_spec",
    "slugify_from",
    "validate_file",
    "validate_inbox_message",
    "validate_ticket",
]
