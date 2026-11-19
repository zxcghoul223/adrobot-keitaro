from decimal import Decimal, InvalidOperation

from app.errors import AppError


def integer_share(value):
    try:
        share = Decimal(str(value))
        if not share.is_finite() or not 0 <= share <= 100 or share != share.to_integral_value():
            raise ValueError
        return int(share)
    except (InvalidOperation, TypeError, ValueError):
        raise AppError(422, "Keitaro принимает только целые доли от 0 до 100%.") from None


def rebalance(offers):
    """Keitaro accepts integer percentages; distribute rounding points by stable offer ID."""
    active = [o for o in offers if o["state"] == "active"]
    if not active:
        raise AppError(422, "В потоке должен остаться хотя бы один активный оффер.")
    if len(active) > 100:
        raise AppError(422, "В одном потоке поддерживается до 100 офферов.")
    pinned = [o for o in active if o["pinned"]]
    free = sorted((o for o in active if not o["pinned"]), key=lambda o: o["offer_id"])
    used = sum(integer_share(o["pinned_share"]) for o in pinned)
    if used > 100 or (not free and used != 100):
        raise AppError(
            422,
            "Закреплённые доли должны укладываться в 100%. "
            "Оставьте незакреплённый оффер для распределения остатка.",
        )
    for offer in offers:
        offer["share"] = 0
    for offer in pinned:
        offer["share"] = integer_share(offer["pinned_share"])
    if free:
        quotient, remainder = divmod(100 - used, len(free))
        for index, offer in enumerate(free):
            offer["share"] = quotient + (index < remainder)
    return offers


def to_keitaro(offers):
    # Keitaro validates both integer values and an upper bound of 100 per offer.
    result = []
    for offer in offers:
        if offer["state"] == "active":
            result.append(
                {
                    "offer_id": offer["offer_id"],
                    "share": integer_share(offer["share"]),
                    "state": offer.get("keitaro_state", "active"),
                }
            )
        elif offer.get("remote_disabled"):
            # A disabled remote relation is not an active percentage or a local deletion.
            # Preserve it until the user explicitly restores (enables) or removes it.
            result.append(
                {
                    "offer_id": offer["offer_id"],
                    "share": integer_share(offer["remote_weight"]),
                    "state": offer["keitaro_state"],
                }
            )
    return result
