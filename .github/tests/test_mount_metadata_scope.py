"""挂载元数据作用域与卡死子进程熔断的源码级不变量检查。

这两条约束都缺少可执行的运行时测试：私有目录属主修复只在真机重定向运行时生效，
挂载子进程卡死又依赖内核态不可中断等待才能复现。因此这里退化为源码级不变量，
防止后续改动重新扩大作用域：让仅映射模式的应用被改写私有目录属主，或让单个应用
的挂载超时连坐整机其它应用的挂载请求。
"""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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


class MountMetadataScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.monitor_roots = read("src/daemon_monitor/roots.rs")
        cls.daemon_mount = read("src/daemon_mount.rs")

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


if __name__ == "__main__":
    unittest.main()
