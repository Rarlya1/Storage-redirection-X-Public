"""挂载元数据作用域、scoped FUSE 失败隔离与根上限分级的源码级不变量检查。

这些约束都缺少可执行的运行时测试：私有目录属主修复只在真机重定向运行时生效，
挂载子进程卡死依赖内核态不可中断等待才能复现，scoped FUSE 单根启动失败与根数
超限降级又依赖具体设备能力和内核版本。因此这里退化为源码级不变量，防止后续
改动重新扩大作用域或收回隔离：让仅映射模式的应用被改写私有目录属主、让单个
应用的挂载超时连坐整机、让单根 FUSE 失败整组回滚，或让二级降级的硬上限低于
顶层目录数从而重新掉进"放弃 scoped FUSE"分支。
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# 二级降级会把各根收敛到所属顶层存储子目录，因此其输出上界等于顶层目录个数：
# `public_collection_name` 列出的 12 个公共集合目录，加上 Android。
TOP_LEVEL_STORAGE_DIR_COUNT = 13


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def function_body(source: str, signature: str) -> str:
    """取出以 `signature` 开头的函数体，按花括号配对，跳过字符串字面量。"""
    start = source.index(signature)
    open_index = source.index("{", start)
    depth = 0
    index = open_index
    in_string = False
    while index < len(source):
        char = source[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_index : index + 1]
        index += 1
    raise AssertionError(f"未找到完整函数体: {signature}")


def const_value(source: str, name: str) -> int:
    match = re.search(rf"{name}: usize = (\d+);", source)
    if match is None:
        raise AssertionError(f"未找到常量定义: {name}")
    return int(match.group(1))


class MountMetadataScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.monitor_roots = read("src/daemon_monitor/roots.rs")
        cls.daemon_mount = read("src/daemon_mount.rs")
        cls.companion_mount = read("src/lifecycle/companion_mount.rs")
        cls.mount_core = read("src/mount/core.rs")
        cls.fuse_mod = read("src/fuse_redirect/mod.rs")
        cls.fuse_config = read("src/fuse_redirect/config.rs")

    def test_mapping_mode_only_apps_skip_private_owner_repair(self) -> None:
        # 仅映射模式在挂载阶段走 apply_path_mappings_only，不执行
        # restore_own_private_directories；监视侧必须同样不建立私有目录属主修复根。
        body = function_body(
            self.monitor_roots,
            "fn build_private_owner_repair_roots(spec: &MonitorAppSpec) -> Vec<WatchRoot>",
        )
        self.assertIn("is_mapping_mode_only", body)

    def test_stuck_mount_circuit_is_scoped_to_one_package(self) -> None:
        # 熔断判据必须带上包名，否则单个应用的挂载超时会连坐整机其它应用。
        skip_body = function_body(
            self.daemon_mount,
            "fn should_skip_for_stuck_children(request: &MountRequest) -> bool",
        )
        self.assertIn("stuck_mount_child_counts(&request.package_name)", skip_body)

        counts_body = function_body(
            self.daemon_mount, "fn stuck_mount_child_counts(package_name: &str) -> (usize, usize)"
        )
        self.assertIn("child.package_name == package_name", counts_body)

    def test_stuck_child_records_expire_and_stay_bounded(self) -> None:
        # 卡死子进程必须记录登记时间（让熔断窗口失效）、记录包名（隔离作用域），
        # 并且回收列表需要有长度上限。
        self.assertIn("struct StuckMountChild", self.daemon_mount)
        self.assertIn("since_ms", self.daemon_mount)
        counts_body = function_body(
            self.daemon_mount, "fn stuck_mount_child_counts(package_name: &str) -> (usize, usize)"
        )
        self.assertIn("STUCK_MOUNT_CHILD_BLOCK_WINDOW_MS", counts_body)
        prune_body = function_body(self.daemon_mount, "fn prune_stuck_mount_children()")
        self.assertIn("MAX_TRACKED_STUCK_MOUNT_CHILDREN", prune_body)

    def test_existing_mapped_directory_keeps_its_metadata(self) -> None:
        # 已存在且属主就是目标 uid 的映射目录必须原样保留，一个元数据系统调用都不发：
        # 无条件 chmod 会把 MediaProvider 维护的既有模式覆盖成固定值，反而让原本能正常
        # 访问自有 Android/data|media|obb/<pkg> 的应用失去写权限。
        body = function_body(
            self.mount_core,
            "pub(super) fn ensure_writable_mapped_directory(&self, path: &str, owner_uid: i32) "
            "-> bool",
        )
        self.assertIn("st.st_uid == uid", body)
        keep_index = body.index("st.st_uid == uid")
        chmod_index = body.index("chmod(c_path.as_ptr(), MAPPED_DIR_MODE)")
        self.assertLess(keep_index, chmod_index)
        # 属主已正确时必须在 chown/chmod 之前直接返回，跳过整段元数据修正。
        self.assertIn("return true", body[keep_index:chmod_index])

    def test_scoped_fuse_start_isolates_single_root_failure(self) -> None:
        # 单根启动失败只丢该根；整组回滚会让一次偶发失败让整个应用失去 FUSE 覆盖，
        # 并把这次失败计入全局能力预算，累计到上限后把所有应用打回 mount namespace。
        # daemon 侧与会共享同一份能力快照的 companion 侧必须保持一致。
        for path in ("src/daemon_mount.rs", "src/lifecycle/companion_mount.rs"):
            body = function_body(read(path), "fn start_scoped_fuse_services(")
            self.assertNotIn("rollback_scoped_fuse_services", body, path)
            self.assertIn("failed_roots", body, path)
            # 全部根都失败时仍要返回 None，保住"FUSE 整体不可用"的降级记账语义。
            self.assertIn("states.is_empty()", body, path)

    def test_scoped_fuse_hard_limit_covers_top_level_directories(self) -> None:
        # 二级降级的输出上界就是顶层目录个数，硬上限必须不小于它，否则二级接不住会
        # 直接掉进三级"放弃 scoped FUSE、退回 mount namespace"分支。
        hard_limit = const_value(self.fuse_mod, "MAX_SCOPED_FUSE_ROOTS")
        self.assertGreaterEqual(hard_limit, TOP_LEVEL_STORAGE_DIR_COUNT)

        target_limit = const_value(self.fuse_mod, "TARGET_SCOPED_FUSE_ROOTS")
        self.assertLessEqual(target_limit, hard_limit)

        # 一级压缩用软目标、二级降级用硬上限，两级判据不能退回同一个常量。
        compact_body = function_body(self.fuse_config, "fn compact_scoped_mount_roots(")
        target_index = compact_body.index("super::TARGET_SCOPED_FUSE_ROOTS")
        hard_index = compact_body.index("super::MAX_SCOPED_FUSE_ROOTS")
        self.assertLess(target_index, hard_index)


if __name__ == "__main__":
    unittest.main()
