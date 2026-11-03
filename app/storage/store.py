"""ProfileStore：画像 / 经历 / 项目的持久化入口。

三个主接口：`load_bundle` 读全量、`upsert_profile` 按 patch 写入、`rollback` 回滚。
配套：经历与项目的 list / upsert / delete，diff 预览，版本历史。

写入铁律：

1. **patch 只合并出现的字段。** 嵌套对象递归合并，数组整体替换，未出现的字段一律不动。
   这样"导入一份解析质量差的简历"不会抹掉用户手填的数据。
2. **每次写入前把旧状态整份存进 audit。** 保留最近 `AUDIT_KEEP` 份；回滚本身也产生
   新版本，历史只增不删，因此可以反复来回滚。
3. **`profile_id` / `updated_at` / `version` 由本层独占**，调用方传入即报错——
   时间戳一被伪造，last-write-wins 的先后判定就失真了。
4. **`skills[].evidence` 必须指向已存在的经历 / 项目 id**，否则拒绝写入。
   确实需要乱序写入（先建技能占位再补条目）时传 `check_evidence=False`。

与其它层的分工：`app/tools` 负责 LLM 参数的宽容归一化，本层对未知字段零容忍。
存储写坏了无法自愈，宁可抛错也不能静默丢弃。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..schema import (
    DEFAULT_PROFILE_ID,
    Experience,
    Profile,
    ProfileBundle,
    Project,
    new_id,
)
from .db import connect, utc_now

# 审计快照保留份数。回滚是低频动作，20 份足够覆盖"最近几次导入"
AUDIT_KEEP = 20

# 由存储层维护、patch 中一律不接受的键名（任意嵌套深度都禁止）
RESERVED_KEYS = frozenset({"profile_id", "updated_at", "version"})


class ProfileStoreError(ValueError):
    """存储层拒绝操作的统一基类，方便上层一次捕获。"""


class ProfileValidationError(ProfileStoreError):
    """patch 含未知字段、时间戳非法、证据悬空、条目仍被引用。"""


class DiffEntry(BaseModel):
    """一处字段级变更。path 用 `basics.location` 这种点号路径，前端可直接定位。"""

    path: str
    old: Any = None
    new: Any = None


class AuditRecord(BaseModel):
    """版本历史的一行。

    语义：版本 `version` 的状态在 `action` 这次写入中被替换掉，发生时间是 `created_at`。
    """

    version: int
    action: str
    created_at: str


class ProfileStore:
    """画像域存储。一个实例持有一个连接，内部用锁串行化，可跨线程复用。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._conn = connect(db_path)
        self._lock = threading.RLock()
        self._profile_id = DEFAULT_PROFILE_ID

    # ------------------------------------------------------------------ 生命周期

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> ProfileStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 读

    def load_bundle(self) -> ProfileBundle:
        """一次取回画像 + 全部经历 + 全部项目。程序启动调这一个就够了。

        库里没有画像行时返回默认空画像（`persisted=False`），不抛错：
        首次运行是正常状态，不是异常。
        """
        with self._lock:
            row = self._fetch_profile_row()
            profile = (
                _validate(Profile, row["data"])
                if row is not None
                else Profile()
            )
            experiences = [
                _validate(Experience, item["data"])
                for item in self._conn.execute(
                    "SELECT data FROM experience WHERE profile_id=? ORDER BY rowid",
                    (self._profile_id,),
                )
            ]
            projects = [
                _validate(Project, item["data"])
                for item in self._conn.execute(
                    "SELECT data FROM project WHERE profile_id=? ORDER BY rowid",
                    (self._profile_id,),
                )
            ]
            return ProfileBundle(
                profile=profile,
                experiences=experiences,
                projects=projects,
                version=int(row["version"]) if row is not None else 0,
                persisted=row is not None,
            )

    def current_version(self) -> int:
        with self._lock:
            row = self._fetch_profile_row()
            return int(row["version"]) if row is not None else 0

    # ------------------------------------------------------------------ 写：画像

    def upsert_profile(
        self,
        patch: Mapping[str, Any],
        *,
        action: str = "manual_edit",
        check_evidence: bool = True,
    ) -> Profile:
        """按 patch 合并写入画像，返回写入后的完整画像。

        patch 的键必须是 Profile 及其子模型的字段名；数组（skills / target_cities /
        my_work ...）整体替换，不做元素级合并——元素级合并需要额外的身份约定，
        而前端的真实交互就是"整份提交新列表"。

        空 patch（或值与现状完全一致）不产生版本，也不留审计。
        """
        with self._lock:
            self._ensure_profile_row()
            before = self._fetch_profile_row()
            current = _validate(Profile, before["data"])
            diffs: list[DiffEntry] = []
            merged = _merge(current.model_dump(), patch, Profile, "", diffs)
            if not diffs:
                return current
            merged["meta"]["updated_at"] = utc_now()
            candidate = _validate(Profile, merged)
            if check_evidence:
                self._check_evidence(candidate)
            self._commit(candidate, action=action)
            return candidate

    def replace_profile(
        self,
        profile: Profile,
        *,
        action: str = "manual_edit",
        check_evidence: bool = True,
    ) -> Profile:
        """整份覆盖。前端全量提交、或导入流程重建画像时用。"""
        with self._lock:
            self._ensure_profile_row()
            data = profile.model_dump()
            data["meta"]["updated_at"] = utc_now()
            candidate = _validate(Profile, data)
            if check_evidence:
                self._check_evidence(candidate)
            self._commit(candidate, action=action)
            return candidate

    def diff_profile(self, patch: Mapping[str, Any]) -> list[DiffEntry]:
        """只算不写：给前端展示"这次导入会改动哪几处、旧值是什么"。

        这是 last-write-wins 下唯一的误覆盖防线——优先级已经删掉了，
        就把决定权交还给用户。
        """
        with self._lock:
            self._ensure_profile_row()
            current = _validate(Profile, self._fetch_profile_row()["data"])
            diffs: list[DiffEntry] = []
            _merge(current.model_dump(), patch, Profile, "", diffs)
            return diffs

    def rollback(self, target_version: int) -> Profile:
        """回滚到某个历史版本。

        回滚 = 用旧快照再写一次，因此它自己也占一个新版本、也留审计，
        可以再滚回来；不删任何历史。
        """
        with self._lock:
            row = self._fetch_profile_row()
            if row is None:
                raise ProfileStoreError("尚无画像，无需回滚")
            current_version = int(row["version"])
            if target_version == current_version:
                return _validate(Profile, row["data"])
            snapshot = self._conn.execute(
                "SELECT data FROM audit WHERE profile_id=? AND version=? ORDER BY id DESC LIMIT 1",
                (self._profile_id, target_version),
            ).fetchone()
            if snapshot is None:
                raise ProfileValidationError(
                    f"版本 {target_version} 的历史不可用（当前 {current_version}，"
                    f"审计只保留最近 {AUDIT_KEEP} 份）"
                )
            data = _validate(Profile, snapshot["data"]).model_dump()
            data["meta"]["updated_at"] = utc_now()
            candidate = _validate(Profile, data)
            # 目标版本里的 evidence 可能因后续删除条目而失效，这里必须复查
            self._check_evidence(candidate, context=f"回滚到版本 {target_version} 时")
            self._commit(candidate, action="rollback")
            return candidate

    def history(self, limit: int = AUDIT_KEEP) -> list[AuditRecord]:
        """版本历史，按版本号倒序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, action, created_at FROM audit WHERE profile_id=? "
                "ORDER BY version DESC LIMIT ?",
                (self._profile_id, limit),
            ).fetchall()
            return [
                AuditRecord(
                    version=int(item["version"]),
                    action=str(item["action"]),
                    created_at=str(item["created_at"]),
                )
                for item in rows
            ]

    def snapshot_at(self, version: int) -> Profile | None:
        """取某历史版本的画像快照，用于前端"对比两个版本"。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM audit WHERE profile_id=? AND version=? ORDER BY id DESC LIMIT 1",
                (self._profile_id, version),
            ).fetchone()
            return _validate(Profile, row["data"]) if row is not None else None

    # ------------------------------------------------------------------ 写：经历

    def list_experiences(self) -> list[Experience]:
        with self._lock:
            return [
                _validate(Experience, item["data"])
                for item in self._conn.execute(
                    "SELECT data FROM experience WHERE profile_id=? ORDER BY rowid",
                    (self._profile_id,),
                )
            ]

    def upsert_experience(self, item: Experience) -> Experience:
        """新增或整体替换一段经历。`exp_id` 留空时自动生成。"""
        return self._upsert_entry("experience", "exp_id", "exp", item)

    def delete_experience(self, exp_id: str, *, force: bool = False) -> Profile | None:
        """删除一段经历。

        默认拒绝删除仍被 `skills[].evidence` 引用的经历，并把引用它的技能名报回去，
        由用户裁决"是技能错了还是经历该删"。`force=True` 才连带剔除引用。
        """
        return self._delete_entry("experience", "exp_id", exp_id, force=force)

    # ------------------------------------------------------------------ 写：项目

    def list_projects(self) -> list[Project]:
        with self._lock:
            return [
                _validate(Project, item["data"])
                for item in self._conn.execute(
                    "SELECT data FROM project WHERE profile_id=? ORDER BY rowid",
                    (self._profile_id,),
                )
            ]

    def upsert_project(self, item: Project) -> Project:
        """新增或整体替换一个项目。`proj_id` 留空时自动生成。"""
        return self._upsert_entry("project", "proj_id", "proj", item)

    def delete_project(self, proj_id: str, *, force: bool = False) -> Profile | None:
        return self._delete_entry("project", "proj_id", proj_id, force=force)

    # ------------------------------------------------------------------ 内部

    def _fetch_profile_row(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT data, version FROM profile WHERE profile_id=?", (self._profile_id,)
        ).fetchone()

    def _ensure_profile_row(self) -> None:
        """画像行必须存在，否则经历 / 项目的外键无处挂靠。"""
        if self._fetch_profile_row() is None:
            self._conn.execute(
                "INSERT INTO profile(profile_id, data, updated_at, version) VALUES(?,?,?,?)",
                (self._profile_id, Profile().model_dump_json(), utc_now(), 0),
            )
            self._conn.commit()

    def _commit(self, profile: Profile, *, action: str) -> int:
        """旧状态留档 → 写入新状态 → 版本号 +1。必须在锁内调用。"""
        row = self._fetch_profile_row()
        now = utc_now()
        new_version = int(row["version"]) + 1 if row is not None else 1
        if row is not None:
            self._conn.execute(
                "INSERT INTO audit(profile_id, version, data, action, created_at) "
                "VALUES(?,?,?,?,?)",
                (self._profile_id, int(row["version"]), row["data"], action, now),
            )
        self._conn.execute(
            # 必须用 ON CONFLICT DO UPDATE，不能用 INSERT OR REPLACE：
            # 后者内部是“先删再插”，会触发子表 ON DELETE CASCADE 把经历 / 项目清空
            "INSERT INTO profile(profile_id, data, updated_at, version) VALUES(?,?,?,?) "
            "ON CONFLICT(profile_id) DO UPDATE SET "
            "data=excluded.data, updated_at=excluded.updated_at, version=excluded.version",
            (self._profile_id, profile.model_dump_json(), profile.meta.updated_at, new_version),
        )
        self._prune_audit()
        self._conn.commit()
        return new_version

    def _prune_audit(self) -> None:
        """滚动删除最老的审计，保留 AUDIT_KEEP 份。"""
        self._conn.execute(
            "DELETE FROM audit WHERE profile_id=? AND id NOT IN ("
            "SELECT id FROM audit WHERE profile_id=? ORDER BY version DESC LIMIT ?)",
            (self._profile_id, self._profile_id, AUDIT_KEEP),
        )

    def _entry_ids(self) -> set[str]:
        ids = {
            str(item["exp_id"])
            for item in self._conn.execute(
                "SELECT exp_id FROM experience WHERE profile_id=?", (self._profile_id,)
            )
        }
        ids |= {
            str(item["proj_id"])
            for item in self._conn.execute(
                "SELECT proj_id FROM project WHERE profile_id=?", (self._profile_id,)
            )
        }
        return ids

    def _check_evidence(self, profile: Profile, *, context: str = "写入时") -> None:
        known = self._entry_ids()
        dangling = [
            f"{skill.name or '(未命名技能)'} → {ref}"
            for skill in profile.skills
            for ref in skill.evidence
            if ref not in known
        ]
        if dangling:
            preview = "; ".join(dangling[:5])
            more = f"（共 {len(dangling)} 处）" if len(dangling) > 5 else ""
            raise ProfileValidationError(
                f"{context} skills[].evidence 指向不存在的条目：{preview}{more}。"
                "请先创建对应经历 / 项目，或传 check_evidence=False"
            )

    def _upsert_entry(
        self, table: str, id_field: str, prefix: str, item: Experience | Project
    ) -> Experience | Project:
        with self._lock:
            self._ensure_profile_row()
            raw = getattr(item, id_field)
            entry_id = raw.strip() if raw and raw.strip() else new_id(prefix)
            record = item.model_copy(update={id_field: entry_id})
            self._conn.execute(
                f"INSERT INTO {table}({id_field}, profile_id, data, updated_at) VALUES(?,?,?,?) "
                f"ON CONFLICT({id_field}) DO UPDATE SET "
                "data=excluded.data, updated_at=excluded.updated_at",
                (entry_id, self._profile_id, record.model_dump_json(), utc_now()),
            )
            self._conn.commit()
            return record

    def _delete_entry(
        self, table: str, id_field: str, entry_id: str, *, force: bool
    ) -> Profile | None:
        with self._lock:
            existed = self._conn.execute(
                f"SELECT 1 FROM {table} WHERE {id_field}=? AND profile_id=?",
                (entry_id, self._profile_id),
            ).fetchone()
            if existed is None:
                raise ProfileValidationError(f"{table} 中不存在条目 {entry_id}")

            bundle = self.load_bundle()
            referencing = bundle.skills_referencing(entry_id)
            if referencing and not force:
                raise ProfileValidationError(
                    f"技能 {', '.join(referencing)} 的证据指向 {entry_id}，"
                    "删除会留下悬空引用；确认无误再传 force=True"
                )
            self._conn.execute(
                f"DELETE FROM {table} WHERE {id_field}=?", (entry_id,)
            )
            if not referencing:
                self._conn.commit()
                return bundle.profile

            # 剔除引用后重新提交画像，顺带留一次审计
            data = bundle.profile.model_dump()
            data["skills"] = [
                {**skill, "evidence": [ref for ref in skill["evidence"] if ref != entry_id]}
                for skill in data["skills"]
            ]
            data["meta"]["updated_at"] = utc_now()
            self._conn.commit()
            return self._commit_and_return(Profile.model_validate(data), action="entry_delete")

    def _commit_and_return(self, profile: Profile, *, action: str) -> Profile:
        self._commit(profile, action=action)
        return profile


def _validate(model_cls: type[Any], payload: Any):
    """把 pydantic 的校验错误转成存储层统一异常，上层不必认识 pydantic。"""
    try:
        if isinstance(payload, str):
            return model_cls.model_validate_json(payload)
        return model_cls.model_validate(payload)
    except ValidationError as error:
        raise ProfileValidationError(f"{model_cls.__name__} 校验失败：{error}") from error


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _merge(
    base: dict[str, Any],
    patch: Mapping[str, Any],
    model_cls: type[BaseModel],
    path: str,
    diffs: list[DiffEntry],
) -> dict[str, Any]:
    """递归合并 patch 到 base，同时记录叶子级差异。

    未知字段直接报错：静默丢弃一个拼错的键，等于让用户以为"改了但没生效"。
    """
    result = dict(base)
    fields = model_cls.model_fields
    for key, value in patch.items():
        full = f"{path}.{key}" if path else key
        if key in RESERVED_KEYS:
            raise ProfileValidationError(f"{full} 由存储层维护，不接受写入")
        if key not in fields:
            raise ProfileValidationError(f"{model_cls.__name__} 没有字段 {key}（{full}）")
        annotation = fields[key].annotation
        if isinstance(value, Mapping) and _is_model(annotation):
            result[key] = _merge(result.get(key) or {}, value, annotation, full, diffs)
            continue
        old = result.get(key)
        if old != value:
            diffs.append(DiffEntry(path=full, old=old, new=value))
            result[key] = value
    return result


__all__ = [
    "AUDIT_KEEP",
    "RESERVED_KEYS",
    "AuditRecord",
    "DiffEntry",
    "ProfileStore",
    "ProfileStoreError",
    "ProfileValidationError",
]


if __name__ == "__main__":
    # 冒烟测试：直接 python -m app.storage.store，全程用临时目录，
    # 绝不读写 ~/.careersassistant/profile.db
    import shutil
    import sys
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    from ..schema import Experience, Outcome, Period, Project
    from .db import DEFAULT_DB_PATH, SchemaVersionError, connect as _connect

    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

    _tmp = Path(tempfile.mkdtemp(prefix="careers_profile_"))
    _db = _tmp / "profile.db"

    def step(title: str) -> None:
        print(f"\n{'=' * 68}\n{title}\n{'-' * 68}")

    try:
        store = ProfileStore(_db)
        print(f"\n默认库路径（本次未使用）：{DEFAULT_DB_PATH}")

        step("1. 首次读取：空画像不应报错")
        bundle = store.load_bundle()
        assert bundle.persisted is False and bundle.version == 0
        assert bundle.profile.basics.name == "" and bundle.experiences == []
        print(f"persisted={bundle.persisted} version={bundle.version} → 空画像就位")

        step("2. 先建经历 / 项目，拿到可被 evidence 引用的 id")
        exp = store.upsert_experience(
            Experience(
                kind="work",
                company="某科技",
                title="后端工程师",
                period=Period(start="2021-07", end="2024-12"),
                my_work=["负责订单服务"],
                outcomes=[Outcome(text="QPS 提升 30%", quantified=True)],
                domains=["toB"],
            )
        )
        proj = store.upsert_project(
            Project(
                name="CareersAssistant",
                summary="求职 Agent",
                role="独立作者",
                my_work=["设计画像 schema"],
                outcomes=[Outcome(text="GitHub 128 stars", quantified=True)],
                capabilities=["Python", "Agent 编排"],
                url="https://github.com/luzhijian1996/CareersAssistant",
            )
        )
        assert exp.exp_id.startswith("exp_") and proj.proj_id.startswith("proj_")
        print(f"exp_id={exp.exp_id} proj_id={proj.proj_id}")
        # id 由存储层补齐后，画像里只存生成好的 id
        assert {e.exp_id for e in store.list_experiences()} == {exp.exp_id}

        step("3. patch 写入画像：skills.evidence 指向真实条目")
        profile = store.upsert_profile(
            {
                "basics": {"name": "张三", "phone": "13800000000", "years_of_experience": 4},
                "preferences": {"target_cities": ["杭州"], "job_type": "fulltime"},
                "skills": [
                    {"name": "Python", "level": "expert", "evidence": [exp.exp_id, proj.proj_id]},
                    {"name": "需求访谈", "level": "familiar", "evidence": [exp.exp_id]},
                ],
                "talk_points": {"self_intro": "四年后端，近两年做 Agent"},
            }
        )
        assert store.current_version() == 1
        assert len(profile.skills) == 2 and profile.talk_points.self_intro
        # 写画像绝不能连带清空条目表（曾经踩过 INSERT OR REPLACE 触发 CASCADE 的坑）
        still = store.load_bundle()
        assert len(still.experiences) == 1 and len(still.projects) == 1, "写画像后条目被级联删除"
        print(f"version=1 name={profile.basics.name} skills={[s.name for s in profile.skills]}")

        step("4. 未出现的字段一律不动（核心铁律）")
        store.upsert_profile({"basics": {"location": "杭州"}})
        reloaded = store.load_bundle()
        assert reloaded.profile.basics.name == "张三", "手填姓名被导入覆盖"
        assert reloaded.profile.basics.phone == "13800000000"
        assert len(reloaded.profile.skills) == 2
        assert reloaded.version == 2
        print(f"只改 location 后：name/phone/skills 全部保留，version={reloaded.version}")

        step("5. 数组整体替换，不做元素级合并")
        store.upsert_profile({"skills": [{"name": "Go", "level": "familiar"}]})
        after = store.load_bundle().profile
        assert [s.name for s in after.skills] == ["Go"]
        print("skills 传 1 条即覆盖为 1 条（旧的两条不残留）")

        step("6. 三类拒绝：未知字段 / 保留字段 / 悬空证据")
        for patch, hint in (
            ({"basics": {"nieig": "李四"}}, "未知字段"),
            ({"meta": {"updated_at": "2020-01-01T00:00:00+00:00"}}, "保留字段"),
            ({"skills": [{"name": "Rust", "evidence": ["ghost_1"]}]}, "悬空证据"),
        ):
            try:
                store.upsert_profile(patch)
            except ProfileValidationError as error:
                print(f"已拦截[{hint}]：{str(error)[:88]}")
            else:
                raise AssertionError(f"{hint} 未被拦截")
        assert store.current_version() == 3, "被拒绝的写入不应产生新版本"

        step("7. diff 预览：算得清改动，但不落库")
        before_version = store.current_version()
        diffs = store.diff_profile({"basics": {"name": "李四"}, "preferences": {"remote_ok": True}})
        assert [(d.path, d.old, d.new) for d in diffs] == [
            ("basics.name", "张三", "李四"),
            ("preferences.remote_ok", False, True),
        ], [d.model_dump() for d in diffs]
        assert store.current_version() == before_version, "预览不应产生版本"
        print(f"预览到 {len(diffs)} 处变更，版本仍为 {before_version}")

        step("8. 空 patch / 同值 patch 不产生版本")
        untouched = store.upsert_profile({})
        assert untouched.model_dump() == store.load_bundle().profile.model_dump()
        same = store.upsert_profile({"basics": {"name": "张三"}})
        assert same.basics.name == "张三" and store.current_version() == before_version
        print(f"空 patch 与重复写同值后 version={store.current_version()}（未增长）")

        step("9. 回滚：用旧快照再写一次，历史只增不删")
        # 审计存的是“被替换掉的那一版”，所以回滚目标始终是已封存的旧版本号
        pre_rollback = store.current_version()
        snapshot = store.snapshot_at(1)
        assert snapshot is not None and snapshot.basics.name == "张三"
        rolled = store.rollback(1)
        assert rolled.basics.name == "张三" and [s.name for s in rolled.skills] == [
            "Python",
            "需求访谈",
        ]
        assert store.current_version() == pre_rollback + 1
        again = store.rollback(pre_rollback)
        assert [s.name for s in again.skills] == ["Go"]
        assert store.current_version() == pre_rollback + 2
        print(
            f"回滚到 v1 内容→新版本 {pre_rollback + 1}；再滚回 v{pre_rollback} 内容"
            f"→新版本 {pre_rollback + 2}，历史一份没删"
        )
        # 回滚到当前版本是空操作，不应新增版本
        noop_version = store.current_version()
        assert store.rollback(noop_version).model_dump() == store.load_bundle().profile.model_dump()
        assert store.current_version() == noop_version
        try:
            store.rollback(999)
        except ProfileValidationError as error:
            print(f"回滚不存在的版本已拦截：{str(error)[:70]}")

        step("10. 删除被引用的条目：默认拒绝，force 才连带剔除")
        store.upsert_profile(
            {"skills": [{"name": "Python", "evidence": [exp.exp_id]}, {"name": "架构", "evidence": [proj.proj_id]}]}
        )
        try:
            store.delete_experience(exp.exp_id)
        except ProfileValidationError as error:
            print(f"已拦截：{str(error)[:88]}")
        else:
            raise AssertionError("悬空引用未被拦截")
        after_force = store.delete_experience(exp.exp_id, force=True)
        assert after_force is not None
        assert exp.exp_id not in store.load_bundle().entry_ids
        kept = {tuple(skill.evidence) for skill in store.load_bundle().profile.skills}
        assert all(exp.exp_id not in refs for refs in kept), kept
        print(f"force 删除成功，剩余引用已剔除：{sorted(kept)}")
        # 条目已不存在，再拿它做证据写入必须被拒
        try:
            store.upsert_profile({"skills": [{"name": "C", "evidence": [exp.exp_id]}]})
        except ProfileValidationError as error:
            print(f"删除后悬空引用已拦截：{str(error)[:70]}")
        else:
            raise AssertionError("删除条目后旧证据未被拦截")

        step("11. 审计滚动保留")
        for index in range(AUDIT_KEEP + 10):
            store.upsert_profile({"basics": {"location": f"城市{index}"}})
        history = store.history()
        assert len(history) <= AUDIT_KEEP, len(history)
        print(f"写了 {AUDIT_KEEP + 10} 次，历史留 {len(history)} 份，最新一条：{history[0]}")

        step("12. 关库重开：数据必须还在（持久化的唯一验收）")
        expect = store.load_bundle()
        store.close()
        reopened = ProfileStore(_db)
        got = reopened.load_bundle()
        assert got.profile.model_dump() == expect.profile.model_dump()
        assert [p.proj_id for p in got.projects] == [p.proj_id for p in expect.projects]
        assert got.version == expect.version and got.persisted is True
        print(f"重开读到 version={got.version} projects={len(got.projects)} exps={len(got.experiences)}")

        step("13. 并发写：Agent 线程池 + 前端同时提交不能坏数据")
        def worker(index: int) -> None:
            for sub in range(5):
                reopened.upsert_profile({"basics": {"location": f"W{index}-{sub}"}})

        version_before = reopened.current_version()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))
        version_after = reopened.current_version()
        assert version_after == version_before + 40, (version_before, version_after)
        assert reopened.load_bundle().profile.basics.location.startswith("W")
        print(f"8 线程 × 5 次：version {version_before} → {version_after}，无丢失无错乱")
        reopened.close()

        step("14. 高版本库文件拒绝打开")
        stale = _tmp / "stale.db"
        fresh = _connect(stale)
        fresh.execute("PRAGMA user_version=99")
        fresh.commit()
        fresh.close()
        try:
            _connect(stale)
        except SchemaVersionError as error:
            print(f"已拦截：{error}")
        else:
            raise AssertionError("降级打开未被拦截")

        print(f"\n{'=' * 68}\n全部通过：patch 合并 / 证据校验 / 回滚 / 审计 / 并发\n{'=' * 68}")
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
        print(f"\n临时目录已清理：{_tmp}")
