"""Поля черновика мастера, которые можно перенести через форму подключения."""
from .view_html import html


FIELDS = (
    "left_kind", "left_path", "left_id", "right_kind", "right_path", "right_id",
    "field_left", "field_right", "field_mode", "field_weight", "field_block_on_conflict",
    "review", "accept", "block_limit",
)


def start_connection(form):
    side = form.get("connect_side")
    if side not in (["left"], ["right"]):
        raise ValueError("Выберите сторону подключения")
    state = {f"draft_{name}": form[name] for name in FIELDS if name in form}
    return {**state, "side": side, "return_to_wizard": ["1"]}


def carried_inputs(form):
    if form.get("return_to_wizard") != ["1"]:
        return ""
    parts = ['<input type="hidden" name="return_to_wizard" value="1">']
    for name in FIELDS:
        key = "draft_" + name
        for value in form.get(key, []):
            parts.append(f'<input type="hidden" name="{key}" value="{html(value)}">')
    return "".join(parts)


def restore_draft(form):
    draft = {name: form["draft_" + name] for name in FIELDS if "draft_" + name in form}
    for side in ("left", "right"):
        profile = form.get(side + "_profile")
        if profile:
            if len(profile) != 1 or len(form.get(side + "_id", [])) != 1:
                raise ValueError("Выберите профиль и идентификатор")
            draft.update({side + "_kind": ["profile"], side + "_path": profile, side + "_id": form[side + "_id"]})
    return draft
