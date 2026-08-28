/**
 * 行级 diff（最长公共子序列）。
 *
 * 只做行级、不做词级：提示词是按句分行写的，行级差异恰好对应"多了/少了哪句约束"，
 * 而词级高亮会把两句改写显示成一堆碎片，反而看不出改了什么。
 *
 * 输入规模是几十行的提示词，所以直接用 O(n*m) 的 DP 表，不做优化。
 */
export type DiffOp = "same" | "added" | "removed";

export interface DiffLine {
    op: DiffOp;
    text: string;
}

export const diffLines = (before: string, after: string): DiffLine[] => {
    const a = before.split("\n");
    const b = after.split("\n");

    // lcs[i][j] = a[i..] 与 b[j..] 的最长公共子序列长度
    const lcs: number[][] = Array.from({ length: a.length + 1 }, () =>
        new Array<number>(b.length + 1).fill(0),
    );
    for (let i = a.length - 1; i >= 0; i--) {
        for (let j = b.length - 1; j >= 0; j--) {
            lcs[i][j] =
                a[i] === b[j]
                    ? lcs[i + 1][j + 1] + 1
                    : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
        }
    }

    const result: DiffLine[] = [];
    let i = 0;
    let j = 0;
    while (i < a.length && j < b.length) {
        if (a[i] === b[j]) {
            result.push({ op: "same", text: a[i] });
            i++;
            j++;
        } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
            result.push({ op: "removed", text: a[i] });
            i++;
        } else {
            result.push({ op: "added", text: b[j] });
            j++;
        }
    }
    while (i < a.length) result.push({ op: "removed", text: a[i++] });
    while (j < b.length) result.push({ op: "added", text: b[j++] });
    return result;
};

export const countDiff = (lines: DiffLine[]) => ({
    added: lines.filter((line) => line.op === "added").length,
    removed: lines.filter((line) => line.op === "removed").length,
});
