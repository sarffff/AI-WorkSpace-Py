import { describe, it, expect } from "vitest";
import { diffLines, countDiff, type DiffLine } from "./diffLines";

/**
 * 提示词版本对比的 diff。这是前端唯一一处真正的算法（LCS 动态规划），
 * 也是最值得先测的地方：它错了不会抛异常，只会把"改了哪句约束"显示错，
 * 而用户正是靠这个界面决定要不要回滚某个提示词版本。
 */

const render = (lines: DiffLine[]) =>
  lines.map((line) => `${line.op[0]}:${line.text}`).join("|");

describe("diffLines", () => {
  it("完全相同的文本全部标 same", () => {
    const result = diffLines("a\nb\nc", "a\nb\nc");
    expect(result).toEqual([
      { op: "same", text: "a" },
      { op: "same", text: "b" },
      { op: "same", text: "c" },
    ]);
  });

  it("中间插入一行只标那一行 added", () => {
    const result = diffLines("a\nc", "a\nb\nc");
    expect(render(result)).toBe("s:a|a:b|s:c");
  });

  it("中间删除一行只标那一行 removed", () => {
    const result = diffLines("a\nb\nc", "a\nc");
    expect(render(result)).toBe("s:a|r:b|s:c");
  });

  it("改一行 = 先 removed 再 added，且保留在原位置", () => {
    // 这条锁的是"顺序"而不只是"内容"：removed 必须在 added 之前，
    // 否则界面上读起来像"先加了新的、再删了旧的"，与编辑直觉相反。
    const result = diffLines("keep\nold\ntail", "keep\nnew\ntail");
    expect(render(result)).toBe("s:keep|r:old|a:new|s:tail");
  });

  it("空字符串对非空：split 产生一个空行，所以是替换而不是纯新增", () => {
    // "".split("\n") === [""]，不是 []。这个 JS 行为决定了这里必然多一个
    // removed 空行；断言写成"纯 added"会在实现正确时失败。
    const result = diffLines("", "a");
    expect(render(result)).toBe("r:|a:a");
  });

  it("两边都空时得到一行 same 空行", () => {
    expect(diffLines("", "")).toEqual([{ op: "same", text: "" }]);
  });

  it("整段替换：删除行整体在新增行之前，不是逐行交错", () => {
    // 没有任何公共行时 lcs 表全是 0，于是 `lcs[i+1][j] >= lcs[i][j+1]` 恒真，
    // 循环一路走 removed 分支，把 a 走完才开始吐 b。结果是"先全删再全加"，
    // 而不是 r:x|a:p|r:y|a:q 那样逐行交错。
    //
    // 这个分组行为对界面是更好的那一种（整段替换看起来就该是一块删一块加），
    // 但它来自 `>=` 里那个等号，改成 `>` 就会翻转。所以钉住它。
    const result = diffLines("x\ny", "p\nq");
    expect(render(result)).toBe("r:x|r:y|a:p|a:q");
  });

  it("重复行不会被错误配对", () => {
    // LCS 对重复行最容易出错：三个 a 变两个 a 应当只删一个。
    const result = diffLines("a\na\na", "a\na");
    expect(countDiff(result)).toEqual({ added: 0, removed: 1 });
    expect(result.filter((l) => l.op === "same")).toHaveLength(2);
  });

  it("保留每一行的原文，不做 trim", () => {
    // 提示词里的缩进是有意义的（示例代码块、列表层级），trim 掉会让
    // diff 显示成"没变"而实际缩进改了。
    const result = diffLines("  indented", "    indented");
    expect(render(result)).toBe("r:  indented|a:    indented");
  });

  it("行内容含特殊字符时按整行比较", () => {
    const before = "约束：不要重复检索\n上限 5 条";
    const after = "约束：不要重复检索\n上限 10 条";
    const result = diffLines(before, after);
    expect(render(result)).toBe(
      "s:约束：不要重复检索|r:上限 5 条|a:上限 10 条",
    );
  });

  it("同一份输入重复调用结果一致（无内部可变状态）", () => {
    const a = "one\ntwo\nthree";
    const b = "one\ntwo-changed\nthree";
    expect(diffLines(a, b)).toEqual(diffLines(a, b));
  });

  it("所有 same 行按顺序拼回来就是公共子序列", () => {
    const before = "h1\nbody\nfooter\ntail";
    const after = "h1\nintro\nbody\ntail";
    const result = diffLines(before, after);
    const same = result.filter((l) => l.op === "same").map((l) => l.text);
    expect(same).toEqual(["h1", "body", "tail"]);
  });

  it("removed 拼回来等于原文，added 拼回来等于新文", () => {
    // 这是 diff 的正确性不变式：比逐条断言更能覆盖未预料的输入。
    const before = "a\nb\nc\nd";
    const after = "a\nx\nc\ny\nz";
    const result = diffLines(before, after);
    const fromBefore = result
      .filter((l) => l.op !== "added")
      .map((l) => l.text)
      .join("\n");
    const fromAfter = result
      .filter((l) => l.op !== "removed")
      .map((l) => l.text)
      .join("\n");
    expect(fromBefore).toBe(before);
    expect(fromAfter).toBe(after);
  });
});

describe("countDiff", () => {
  it("分别数 added 与 removed，不数 same", () => {
    const lines: DiffLine[] = [
      { op: "same", text: "a" },
      { op: "added", text: "b" },
      { op: "added", text: "c" },
      { op: "removed", text: "d" },
    ];
    expect(countDiff(lines)).toEqual({ added: 2, removed: 1 });
  });

  it("空输入是两个零", () => {
    expect(countDiff([])).toEqual({ added: 0, removed: 0 });
  });
});
