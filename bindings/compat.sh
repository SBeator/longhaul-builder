#!/usr/bin/env bash
# longhaul-builder — 跨平台兼容 shim（source 进各 binding 用）。
# 设计原则：**Linux 行为零改动**——有 GNU 工具就照用原逻辑；只有在缺失（macOS/BSD）时才回退。
#
# 提供：
#   lhb_timeout <duration> <cmd...>   GNU `timeout` → `gtimeout` → perl alarm 回退（退出码 124=超时，与 GNU 一致）
#
# 数组安全：调用方用 `${ARR[@]+"${ARR[@]}"}` 展开可能为空的数组——在现代 bash(Linux) 与 bash 3.2(macOS 自带)
# 行为一致，且不会在 `set -u` 下因"空数组未绑定"报错（bash 3.2 的老坑）。

# 跑命令并施加超时。优先 GNU coreutils 的 timeout / gtimeout；都没有时用 perl 的 alarm 实现。
lhb_timeout() {
  _lhb_to_dur="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$_lhb_to_dur" "$@"; return $?
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$_lhb_to_dur" "$@"; return $?
  fi
  # 纯回退：perl（macOS 自带）。fork 子进程执行命令，alarm 到点 TERM→KILL，超时退 124。
  perl -e '
    my $d = shift @ARGV; $d =~ s/[sS]$//;            # 容忍 "900" 或 "900s"
    $d =~ s/m$/*60/e; $d =~ s/h$/*3600/e;             # 容忍 m/h 后缀
    my $pid = fork(); die "fork failed: $!" unless defined $pid;
    if ($pid == 0) { exec @ARGV or exit 127; }
    my $timed_out = 0;
    local $SIG{ALRM} = sub { $timed_out = 1; kill "TERM", $pid; };
    alarm($d);
    waitpid($pid, 0);
    my $code = $? >> 8; my $sig = $? & 127;
    alarm(0);
    if ($timed_out) { kill "KILL", $pid; exit 124; }
    exit($sig ? 128 + $sig : $code);
  ' "$_lhb_to_dur" "$@"
}
