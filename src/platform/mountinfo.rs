//! `/proc/<pid>/mountinfo` 单行解析。
//!
//! 挂载清理、挂载规划与 scoped FUSE 会话收尾都要按挂载点判断"这条挂载记录是什么"，
//! 此前各模块各写了一遍单行解析。这里提供共享的最小解析：字段保持借用，由调用方决定
//! 展开哪些转义字段，避免为未命中的记录分配字符串。

/// `/proc/self/mountinfo` 中一条挂载记录。
pub struct MountInfoEntry<'a> {
    /// 挂载 ID，同一 namespace 内递增，可用来判断同路径多层挂载的先后。
    pub mount_id: u64,
    /// 挂载在文件系统内的根，未展开八进制转义。
    pub root: &'a str,
    /// 挂载点，未展开八进制转义。
    pub target: &'a str,
    /// 文件系统类型：内核直挂的 scoped 挂载是 `fuse`，经 fusermount 回退时是 `fuse.srx`。
    pub fs_type: &'a str,
    /// 挂载源，即 `mount(2)` 的 `source`。
    pub source: &'a str,
}

/// 解析一行 `mountinfo`；字段不足或挂载 ID 无法解析时返回 None。
pub fn parse_entry(line: &str) -> Option<MountInfoEntry<'_>> {
    let separator = line.find(" - ")?;
    let mut before_fields = line[..separator].split_whitespace();
    let mount_id = before_fields.next()?.parse::<u64>().ok()?;
    before_fields.next()?;
    before_fields.next()?;
    let root = before_fields.next()?;
    let target = before_fields.next()?;
    let mut after_fields = line[separator + 3..].split_whitespace();
    let fs_type = after_fields.next()?;
    let source = after_fields.next()?;
    Some(MountInfoEntry {
        mount_id,
        root,
        target,
        fs_type,
        source,
    })
}

/// 展开 `mountinfo` 字段里的八进制转义（空格、换行、反斜杠等）。
pub fn unescape_field(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(value.len());
    let mut index = 0usize;
    while index < bytes.len() {
        if bytes[index] == b'\\' && index + 3 < bytes.len() {
            let digits = &bytes[index + 1..index + 4];
            if digits.iter().all(|ch| (b'0'..=b'7').contains(ch)) {
                let code = (digits[0] - b'0') * 64 + (digits[1] - b'0') * 8 + (digits[2] - b'0');
                out.push(code);
                index += 4;
                continue;
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}
