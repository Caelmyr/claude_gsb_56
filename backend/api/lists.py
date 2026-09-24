"""自定义刷题清单 API（按用户分片存储，用户间完全隔离）。

存储布局：data/lists/<user_id>/<list_id>.json
每个用户只能读写自己目录下的题单（目录由 token 中的 user_id 决定）。

题单结构：
{
  "id": "l...", "owner": "<user_id>",
  "title": "...", "description": "...",
  "items": [
    {"problem_id": "p...", "added_at": "...", "done": false, "done_at": null}
  ],
  "created_at": "...", "updated_at": "..."
}
题目按 items 数组的固定顺序刷，done 由用户手动标记。
"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth
from backend.storage import read_json, atomic_write_json, locked_update, list_files
from backend.utils import now_iso, gen_id, sanitize_id

lists_bp = Blueprint("lists", __name__)


def _user_dir(user_id):
    """用户题单目录（user_id 来自已签发 token，仍做一次清洗防穿越）。"""
    return os.path.join(config.LISTS_DIR, sanitize_id(user_id))


def _list_path(user_id, list_id):
    return os.path.join(_user_dir(user_id), f"{sanitize_id(list_id)}.json")


def _load_owned(user_id, list_id):
    """读取当前用户拥有的题单，不存在或不属于该用户时返回 None。"""
    lst = read_json(_list_path(user_id, list_id))
    if lst and lst.get("owner") == user_id:
        return lst
    return None


def _problem_brief(problem_id):
    """题目简要信息；题目已被删除时标记 missing。"""
    p = read_json(os.path.join(config.PROBLEMS_DIR, f"{sanitize_id(problem_id)}.json"))
    if not p:
        return {"problem_id": problem_id, "title": "（题目已删除）",
                "missing": True, "difficulty": None, "tags": []}
    return {
        "problem_id": p.get("id", problem_id),
        "title": p.get("title", ""),
        "difficulty": p.get("difficulty"),
        "tags": p.get("tags", []),
        "missing": False,
    }


def _decorate(lst, detail=False):
    """附加进度统计；detail=True 时为每道题附上题目信息。"""
    items = lst.get("items", [])
    out = {
        "id": lst.get("id"),
        "title": lst.get("title", ""),
        "description": lst.get("description", ""),
        "item_count": len(items),
        "done_count": sum(1 for it in items if it.get("done")),
        "created_at": lst.get("created_at"),
        "updated_at": lst.get("updated_at"),
    }
    if detail:
        out["items"] = []
        for idx, it in enumerate(items):
            brief = _problem_brief(it.get("problem_id"))
            brief.update({
                "order": idx + 1,
                "done": bool(it.get("done")),
                "done_at": it.get("done_at"),
                "added_at": it.get("added_at"),
            })
            out["items"].append(brief)
    return out


@lists_bp.get("/lists")
@require_auth
def my_lists():
    uid = request.user["id"]
    result = []
    for lid in list_files(_user_dir(uid)):
        lst = _load_owned(uid, lid)
        if lst:
            result.append(_decorate(lst))
    result.sort(key=lambda l: l.get("updated_at") or l.get("created_at") or "", reverse=True)
    return ok({"total": len(result), "items": result})


@lists_bp.post("/lists")
@require_auth
def create_list():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return err("题单名称不能为空", 400)
    uid = request.user["id"]
    list_id = gen_id("l")
    now = now_iso()
    lst = {
        "id": list_id,
        "owner": uid,
        "title": title[:100],
        "description": (data.get("description") or "").strip()[:2000],
        "items": [],
        "created_at": now,
        "updated_at": now,
    }
    atomic_write_json(_list_path(uid, list_id), lst)
    return ok(_decorate(lst, detail=True))


@lists_bp.get("/lists/<list_id>")
@require_auth
def get_list(list_id):
    lst = _load_owned(request.user["id"], list_id)
    if not lst:
        return err("题单不存在", 404)
    return ok(_decorate(lst, detail=True))


@lists_bp.put("/lists/<list_id>")
@require_auth
def update_list(list_id):
    uid = request.user["id"]
    data = request.get_json(silent=True) or {}

    def _upd(lst):
        if not lst or lst.get("owner") != uid:
            return lst
        if "title" in data:
            title = (data.get("title") or "").strip()
            if not title:
                raise ValueError("题单名称不能为空")
            lst["title"] = title[:100]
        if "description" in data:
            lst["description"] = (data.get("description") or "").strip()[:2000]
        lst["updated_at"] = now_iso()
        return lst

    path = _list_path(uid, list_id)
    if not os.path.exists(path):
        return err("题单不存在", 404)
    try:
        lst = locked_update(path, _upd)
    except ValueError as e:
        return err(str(e), 400)
    if not lst or lst.get("owner") != uid:
        return err("题单不存在", 404)
    return ok(_decorate(lst, detail=True))


@lists_bp.delete("/lists/<list_id>")
@require_auth
def delete_list(list_id):
    uid = request.user["id"]
    path = _list_path(uid, list_id)
    lst = _load_owned(uid, list_id)
    if not lst:
        return err("题单不存在", 404)
    os.remove(path)
    return ok()


@lists_bp.post("/lists/<list_id>/items")
@require_auth
def add_item(list_id):
    uid = request.user["id"]
    data = request.get_json(silent=True) or {}
    problem_id = sanitize_id((data.get("problem_id") or "").strip())
    if not problem_id:
        return err("缺少题目编号", 400)
    if not read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json")):
        return err("题目不存在", 404)

    path = _list_path(uid, list_id)
    if not _load_owned(uid, list_id):
        return err("题单不存在", 404)
    error = [None]

    def _upd(lst):
        if not lst or lst.get("owner") != uid:
            return lst
        if any(it.get("problem_id") == problem_id for it in lst.get("items", [])):
            error[0] = "该题目已在题单中"
            return lst
        lst.setdefault("items", []).append({
            "problem_id": problem_id,
            "added_at": now_iso(),
            "done": False,
            "done_at": None,
        })
        lst["updated_at"] = now_iso()
        return lst

    lst = locked_update(path, _upd)
    if error[0]:
        return err(error[0], 400)
    return ok(_decorate(lst, detail=True))


@lists_bp.delete("/lists/<list_id>/items/<problem_id>")
@require_auth
def remove_item(list_id, problem_id):
    uid = request.user["id"]
    pid = sanitize_id(problem_id)
    path = _list_path(uid, list_id)
    if not _load_owned(uid, list_id):
        return err("题单不存在", 404)

    def _upd(lst):
        if not lst or lst.get("owner") != uid:
            return lst
        before = len(lst.get("items", []))
        lst["items"] = [it for it in lst.get("items", [])
                        if it.get("problem_id") != pid]
        if len(lst["items"]) != before:
            lst["updated_at"] = now_iso()
        return lst

    lst = locked_update(path, _upd)
    return ok(_decorate(lst, detail=True))


@lists_bp.put("/lists/<list_id>/items/<problem_id>")
@require_auth
def set_item_done(list_id, problem_id):
    uid = request.user["id"]
    pid = sanitize_id(problem_id)
    data = request.get_json(silent=True) or {}
    done = bool(data.get("done"))
    path = _list_path(uid, list_id)
    if not _load_owned(uid, list_id):
        return err("题单不存在", 404)
    found = [False]

    def _upd(lst):
        if not lst or lst.get("owner") != uid:
            return lst
        for it in lst.get("items", []):
            if it.get("problem_id") == pid:
                found[0] = True
                if bool(it.get("done")) != done:
                    it["done"] = done
                    it["done_at"] = now_iso() if done else None
                    lst["updated_at"] = it["done_at"]
        return lst

    lst = locked_update(path, _upd)
    if not found[0]:
        return err("题目不在该题单中", 404)
    return ok(_decorate(lst, detail=True))


@lists_bp.put("/lists/<list_id>/reorder")
@require_auth
def reorder_items(list_id):
    """按给定的 problem_id 顺序重排（未列出的题目丢弃，多余/非法 id 忽略）。"""
    uid = request.user["id"]
    data = request.get_json(silent=True) or {}
    order = data.get("order")
    if not isinstance(order, list):
        return err("缺少 order 数组", 400)
    ordered_pids = [sanitize_id(str(x).strip()) for x in order if str(x).strip()]
    path = _list_path(uid, list_id)
    if not _load_owned(uid, list_id):
        return err("题单不存在", 404)

    def _upd(lst):
        if not lst or lst.get("owner") != uid:
            return lst
        by_pid = {it.get("problem_id"): it for it in lst.get("items", [])}
        new_items = [by_pid[p] for p in ordered_pids if p in by_pid]
        # order 中重复出现的 id 只保留一次
        seen = set()
        deduped = []
        for it in new_items:
            if it["problem_id"] in seen:
                continue
            seen.add(it["problem_id"])
            deduped.append(it)
        lst["items"] = deduped
        lst["updated_at"] = now_iso()
        return lst

    lst = locked_update(path, _upd)
    return ok(_decorate(lst, detail=True))
