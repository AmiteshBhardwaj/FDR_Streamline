from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def user_name(user):
    """Resolve display names without dereferencing nullable audit/decision actors."""
    if not user:
        return ""
    full_name = getattr(user, "get_full_name", None)
    return (full_name().strip() if callable(full_name) else "") or getattr(user, "username", "")


@register.filter
def money(value):
    """Format exact rupee amounts with Indian digit grouping."""
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return "—"
    sign = "−" if amount < 0 else ""
    whole, fraction = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        tail, head = whole[-3:], whole[:-3]
        groups = []
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{fraction}"


@register.filter
def label(value):
    return str(value or "Not recorded").replace("_", " ").replace("-", " ").capitalize()


@register.filter
def initials(value):
    words = str(value or "User").split()
    return "".join(word[0] for word in words[:2]).upper()


@register.simple_tag(takes_context=True)
def page_query(context, page):
    query = context["request"].GET.copy()
    query["page"] = page
    return query.urlencode()
