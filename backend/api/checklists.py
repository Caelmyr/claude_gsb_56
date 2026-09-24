"""自定义刷题清单 API。

每个用户（普通用户与管理员）各自维护自己的题单，数据按用户分目录隔离：
    data/checklists/<user_id>/<list_id>.json

题单内的题目以有序列表保存，可按固定顺序刷题、标记完成状态、调整顺序。
"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth
from backend.storage import read_json, atomic_write_json, locked_update, list_files
from backend.utils import now_iso, gen_id, sanitize_id, truncate

checklists_bp = Blueprint("checklists", __name__)

MAX_NAME_LEN = 80
MAX_DESC_LEN = 500
MAX_ITEMS = 500


# ---- 存储辅助 ----

def _user_dir(user_id):
    return os.path.join(config.CHECKLISTS_DIR, sanitize_id(user_id))


def _list_path(user_id, list_id):
    return os.path.join(_user_dir(user_id), f"{sanitize_id(list_id)}.json")


def _load_owned(user, list_id):
    """读取当前用户自己的题单；不存在或不属于该用户均返回 None。"""
    return read_json(_list_path(user["id"], list_id))


def _problem_brief(problem_id):
    """题单展示所需的题目摘要；题目已被删除时返回 None。"""
    p = read_json(os.path.join(config.PROBLEMS_DIR, f"{sanitize_id(problem_id)}.json"))
    if not p:
        return None
    return {
        "id": p.get("id"),
        "title": p.get("title", ""),
        "difficulty": p.get("difficulty", 1),
        "tags": p.get("tags", []),
    }


def _decorate(cl):
    """附加完成进度，并把题目快照信息补全（题目改名/删除时与当前题库对齐）。"""
    out = dict(cl)
    items = out.get("items", [])
    done = 0
    for i, item in enumerate(items):
        brief = _problem_brief(item.get("problem_id"))
        if brief is None:
            item["missing"] = True                       # 题目已从题库删除
        else:
            item.pop("missing", None)
            item["title"] = brief["title"]               # 以题库当前信息为准
            item["difficulty"] = brief["difficulty"]
            item["tags"] = brief["tags"]
        if item.get("done"):
            done += 1
        item["order"] = i + 1
    out["done_count"] = done
    out["total_count"] = len(items)
    return out


def _summary(cl):
    """题单列表用的轻量摘要。"""
    items = cl.get("items", [])
    return {
        "id": cl["id"],
        "name": cl.get("name", ""),
        "description": cl.get("description", ""),
        "created_at": cl.get("created_at"),
        "updated_at": cl.get("updated_at"),
        "total_count": len(items),
        "done_count": sum(1 for it in items if it.get("done")),
    }


# ---- 题单 CRUD ----

@checklists_bp.get("/checklists")
@require_auth
def list_checklists():
    uid = request.user["id"]
    items = []
    for lid in list_files(_user_dir(uid)):
        cl = read_json(_list_path(uid, lid))
        if cl:
            items.append(_summary(cl))
    items.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return ok({"total": len(items), "items": items})


@checklists_bp.post("/checklists")
@require_auth
def create_checklist():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return err("题单名称不能为空", 400)
    cl = {
        "id": gen_id("l"),
        "owner": request.user["id"],
        "name": truncate(name, MAX_NAME_LEN),
        "description": truncate((data.get("description") or "").strip(), MAX_DESC_LEN),
        "items": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    atomic_write_json(_list_path(request.user["id"], cl["id"]), cl)
    return ok(_decorate(cl))


@checklists_bp.get("/checklists/<list_id>")
@require_auth
def get_checklist(list_id):
    cl = _load_owned(request.user, list_id)
    if not cl:
        return err("题单不存在", 404)
    return ok(_decorate(cl))


@checklists_bp.put("/checklists/<list_id>")
@require_auth
def update_checklist(list_id):
    path = _list_path(request.user["id"], list_id)
    if not os.path.exists(path):
        return err("题单不存在", 404)
    data = request.get_json(silent=True) or {}

    def _upd(cl):
        if not cl:
            return cl
        if "name" in data:
            name = (data.get("name") or "").strip()
            if not name:
                raise ValueError("题单名称不能为空")
            cl["name"] = truncate(name, MAX_NAME_LEN)
        if "description" in data:
            cl["description"] = truncate((data.get("description") or "").strip(), MAX_DESC_LEN)
        cl["updated_at"] = now_iso()
        return cl

    try:
        cl = locked_update(path, _upd)
    except ValueError as e:
        return err(str(e), 400)
    return ok(_decorate(cl))


@checklists_bp.delete("/checklists/<list_id>")
@require_auth
def delete_checklist(list_id):
    path = _list_path(request.user["id"], list_id)
    if not os.path.exists(path):
        return err("题单不存在", 404)
    os.remove(path)
    return ok()


# ---- 题单内题目管理 ----

@checklists_bp.post("/checklists/<list_id>/items")
@require_auth
def add_item(list_id):
    path = _list_path(request.user["id"], list_id)
    if not os.path.exists(path):
        return err("题单不存在", 404)
    data = request.get_json(silent=True) or {}
    problem_id = (data.get("problem_id") or "").strip()
    if not problem_id:
        return err("题目编号不能为空", 400)
    problem_id = sanitize_id(problem_id)
    if not _problem_brief(problem_id):
        return err("题目不存在", 404)

    def _upd(cl):
        if not cl:
            return cl
        if any(it.get("problem_id") == problem_id for it in cl.get("items", [])):
            raise ValueError("该题目已在题单中")
        if len(cl.get("items", [])) >= MAX_ITEMS:
            raise ValueError(f"单个题单最多包含 {MAX_ITEMS} 道题")
        cl.setdefault("items", []).append({
            "problem_id": problem_id,
            "done": False,
            "added_at": now_iso(),
            "done_at": None,
        })
        cl["updated_at"] = now_iso()
        return cl

    try:
        cl = locked_update(path, _upd)
    except ValueError as e:
        return err(str(e), 400)
    return ok(_decorate(cl))


@checklists_bp.delete("/checklists/<list_id>/items/<problem_id>")
@require_auth
def remove_item(list_id, problem_id):
    path = _list_path(request.user["id"], list_id)
    if not os.path.exists(path):
        return err("题单不存在", 404)
    pid = sanitize_id(problem_id)

    def _upd(cl):
        if not cl:
            return cl
        before = len(cl.get("items", []))
        cl["items"] = [it for it in cl.get("items", []) if it.get("problem_id") != pid]
        if len(cl["items"]) != before:
            cl["updated_at"] = now_iso()
        return cl

    cl = locked_update(path, _upd)
    return ok(_decorate(cl))


@checklists_bp.put("/checklists/<list_id>/items/<problem_id>/done")
@require_auth
def set_item_done(list_id, problem_id):
    """标记 / 取消标记某道题的完成情况。"""
    path = _list_path(request.user["id"], list_id)
    if not os.path.exists(path):
        return err("题单不存在", 404)
    data = request.get_json(silent=True) or {}
    done = bool(data.get("done", True))
    pid = sanitize_id(problem_id)

    def _upd(cl):
        if not cl:
            return cl
        for it in cl.get("items", []):
            if it.get("problem_id") == pid:
                it["done"] = done
                it["done_at"] = now_iso() if done else None
                cl["updated_at"] = now_iso()
                break
        return cl

    cl = locked_update(path, _upd)
    return ok(_decorate(cl))


@checklists_bp.put("/checklists/<list_id>/reorder")
@require_auth
def reorder_items(list_id):
    """整体重排：请求体 order 为按新顺序排列的题目编号数组。"""
    path = _list_path(request.user["id"], list_id)
    if not os.path.exists(path):
        return err("题单不存在", 404)
    data = request.get_json(silent=True) or {}
    order = data.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        return err("order 必须是题目编号数组", 400)
    order = [sanitize_id(x) for x in order]

    def _upd(cl):
        if not cl:
            return cl
        items = cl.get("items", [])
        by_pid = {}
        for it in items:
            by_pid[it.get("problem_id")] = it
        pids = list(by_pid.keys())
        if sorted(order) != sorted(pids):
            raise ValueError("order 必须且只能包含题单内的全部题目")
        cl["items"] = [by_pid[pid] for pid in order]
        cl["updated_at"] = now_iso()
        return cl

    try:
        cl = locked_update(path, _upd)
    except ValueError as e:
        return err(str(e), 400)
    return ok(_decorate(cl))
