import React, { useEffect, useRef, useState } from "react";
import { RetrievalDebugger } from "@/features/knowledge-debug/ui/RetrievalDebugger";
import { apiClient } from "@/shared/api/client";
import type {
    DocumentVisibility,
    FileTypesCapability,
    KnowledgeDocument,
    WorkspaceInfo,
} from "@/shared/types/api.types";
import { PageHeader } from "@/shared/ui/PageHeader";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import {
    Upload,
    FileText,
    Search,
    ShieldCheck,
    Trash2,
    Loader2,
    X,
    FlaskConical,
    Layers,
    Lock,
    EyeOff,
    UserMinus,
    Users,
} from "lucide-react";
import { StatCard } from "../components/StatCard";
import { WorkspacePanel } from "../components/WorkspacePanel";

function formatSize(bytes: number): string {
    if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
    if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(0)} KB`;
    return `${bytes} 字节`;
}

const DOCUMENT_STATUS_LABELS: Record<KnowledgeDocument["status"], string> = {
    indexed: "已完成",
    processing: "处理中",
    failed: "处理失败",
};

export const KnowledgePage: React.FC = () => {
    const [activeTab, setActiveTab] = useState<"documents" | "debug">("documents");
    const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
    const [totalDocs, setTotalDocs] = useState(0);
    const [totalChunks, setTotalChunks] = useState(0);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(false);
    const [searchQuery, setSearchQuery] = useState("");
    const [deletingId, setDeletingId] = useState<string | null>(null);
    const [errorMsg, setErrorMsg] = useState<string | null>(null);
    const [dragOver, setDragOver] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const toast = useToast();
    const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
    /**
     * 按归属筛。理由是列表规模：admin 现在看得到全体成员的个人文档，
     * 20 个人各传 5 份就有 100 份个人文档把共享库埋掉，而他要维护的恰好是共享那批。
     * 按文件名搜解决不了这个——他不知道该搜什么。
     */
    const [scopeFilter, setScopeFilter] = useState<
        "all" | "shared" | "mine" | "member" | "inherited"
    >("all");
    /**
     * 知识库收哪些格式，由后端给（`services/file_types.py`）。
     *
     * 这个页面此前写死的那份 accept 有两处错：含 `.html`——后端白名单里没有，
     * 于是选得到、传上去 400；缺 csv/log/sh/java/go/rs/c/cpp——后端明明支持，
     * 用户在文件选择器里却选不到。两处都是"两份清单必须一致但不一致时零报错"。
     */
    const [fileTypes, setFileTypes] = useState<FileTypesCapability | null>(null);

    /**
     * 权限判据用后端算好的 isAdmin，不自己比 role：
     * 存量账号上还可能是历史值 "member"，比字符串会漏掉那一档。
     * 旧后端不返回这个字段时退回比 role，那时只有 "admin" 一种非默认值。
     */
    const isAdmin = workspace?.isAdmin ?? workspace?.role === "admin";
    /**
     * 这次上传落在哪。默认共享——这个页面的用途就是维护团队资产。
     * 非 admin 只能选个人，所以直接钉在 private（后端也会挡，这里只是别让人
     * 选一个必然 403 的选项）。
     */
    const [uploadVisibility, setUploadVisibility] =
        useState<DocumentVisibility>("workspace");
    const effectiveVisibility: DocumentVisibility = isAdmin
        ? uploadVisibility
        : "private";

    useEffect(() => {
        apiClient
            .getWorkspace()
            .then(setWorkspace)
            .catch((e) => {
                toast.error(toastMessageFrom(e, "加载工作区信息失败"));
            });
        // 支持的格式随后端配置固定，挂载时取一次就够。取不到不提示：
        // 拖拽上传这条路不依赖它（后端 400 会带回允许列表），
        // 只有文件选择器的过滤器会退化成"不过滤"。
        apiClient
            .getSettings()
            .then((settings) => setFileTypes(settings.capabilities?.fileTypes ?? null))
            .catch(() => setFileTypes(null));
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const refreshDocuments = (showSpinner = true) => {
        if (showSpinner) setLoading(true);
        apiClient
            .getDocuments()
            .then((docs) => {
                setDocuments(docs);
                setTotalDocs(docs.length);
                setTotalChunks(docs.reduce((sum, d) => sum + d.chunks, 0));
            })
            .catch((e) => {
                // 轮询（showSpinner=false，每 2s 一次）失败保持安静，只有
                // 首次加载和手动刷新才提示，否则处理中的文档会刷屏
                if (showSpinner) {
                    toast.error(toastMessageFrom(e, "加载知识库文档失败"));
                }
            })
            .finally(() => {
                if (showSpinner) setLoading(false);
            });
    };

    useEffect(() => {
        refreshDocuments();
    }, []);

    const hasProcessing = documents.some((doc) => doc.status === "processing");
    useEffect(() => {
        if (!hasProcessing) return;
        const timer = setInterval(() => refreshDocuments(false), 2000);
        return () => clearInterval(timer);
    }, [hasProcessing]);

    const handleUploadClick = () => {
        fileInputRef.current?.click();
    };

    const uploadFile = async (file: File) => {
        // 不再拦非 admin：一般用户可以上传**个人文档**。
        // 只有共享文档要 admin，而那个判断在 effectiveVisibility 里。
        setUploading(true);
        setErrorMsg(null);
        try {
            const res = await apiClient.uploadDocument(file, effectiveVisibility);
            if (res.duplicate) {
                toast.info("该文档已在知识库中，未重复索引");
            } else if (effectiveVisibility === "private") {
                toast.info("已存为个人文档，只有你能检索到");
            }
            refreshDocuments();
        } catch (err) {
            setErrorMsg(err instanceof Error ? err.message : "上传失败");
        } finally {
            setUploading(false);
        }
    };

    const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        e.target.value = "";
        await uploadFile(file);
    };

    const handleDrop = async (e: React.DragEvent) => {
        e.preventDefault();
        setDragOver(false);
        const file = e.dataTransfer.files?.[0];
        if (file) await uploadFile(file);
    };

    /**
     * 能不能删这一篇。判据在**文档上**而不是角色上：
     * 个人文档只有本人能删（admin 也不行——那是"只有我看得见"这个承诺的一部分），
     * 共享文档要 admin。
     * 和后端 workspace_service.require_can_modify 是同一套规则；
     * 前端这份只决定按钮给不给点，真正的闸门在后端。
     *
     * admin 现在能在列表里看到成员的个人文档，那些正好落在这个规则的
     * `isOwn === true` 之外——所以不需要额外分支，但要留一句说明：
     * 那不是漏了个按钮，是有意不给。
     */
    const canDelete = (doc: KnowledgeDocument) =>
        doc.visibility === "private" ? doc.isOwn === true : isAdmin;

    /**
     * 三类文档。分类的意义在于它们**能做的事不一样**：
     * shared 归 admin 管，mine 归自己管，member 只能看（admin 才会见到）。
     */
    const classify = (doc: KnowledgeDocument): "shared" | "mine" | "member" => {
        if (doc.visibility !== "private") return "shared";
        return doc.isOwn ? "mine" : "member";
    };

    const counts = {
        all: documents.length,
        shared: documents.filter((d) => classify(d) === "shared").length,
        mine: documents.filter((d) => classify(d) === "mine").length,
        member: documents.filter((d) => classify(d) === "member").length,
        // 离职者留下的、被收编成共享的文档。单独一档是因为它们**需要一个决定**
        // （删还是留），而其他共享文档不需要——admin 得能把它们一次性找出来。
        inherited: documents.filter((d) => d.inherited).length,
    };

    /**
     * 分块总数要分两半报。
     *
     * admin 的列表里含成员的个人文档，而后端不让那些进他的检索
     * （`retrievable: false`）。只显示一个总数会把"库里有多少块"和
     * "我问话时能引用多少块"混成一件事，而它们现在真的不一样了。
     * `retrievable` 缺省 true 是为了兼容旧后端——那时两者本来就相等。
     */
    const retrievableChunks = documents
        .filter((d) => d.retrievable !== false)
        .reduce((sum, d) => sum + d.chunks, 0);
    const unretrievableChunks = totalChunks - retrievableChunks;

    const handleDelete = async (docId: string) => {
        const doc = documents.find((d) => d.id === docId);
        if (doc && !canDelete(doc)) {
            toast.error(
                doc.visibility === "private"
                    ? "这是他人的个人文档，你没有权限删除"
                    : "共享文档仅工作区管理员可以删除",
            );
            return;
        }
        if (!window.confirm("确定删除该文档及其向量分块？")) return;
        setDeletingId(docId);
        setErrorMsg(null);
        try {
            await apiClient.deleteDocument(docId);
            refreshDocuments();
        } catch (err) {
            setErrorMsg(err instanceof Error ? err.message : "删除失败");
        } finally {
            setDeletingId(null);
        }
    };

    const byScope =
        scopeFilter === "all"
            ? documents
            : scopeFilter === "inherited"
              ? // 「继承」和另外三档不是同一个维度：它是共享的一个子集，
                // 而不是第四种归属。放进同一排筛选器是因为对 admin 来说
                // 它们是同一个问题——"这批文档我要不要管"。
                documents.filter((d) => d.inherited)
              : documents.filter((d) => classify(d) === scopeFilter);
    const filteredDocs = searchQuery.trim()
        ? byScope.filter((d) =>
              d.name.toLowerCase().includes(searchQuery.trim().toLowerCase()),
          )
        : byScope;

    return (
        <div className="page-shell app-atmosphere transition-colors duration-200">
            <div className="relative z-10 space-y-6 max-w-6xl">
                <PageHeader
                    eyebrow="检索"
                    title="知识库与 RAG"
                    description="文档进库、切块、混合检索。调试页不经过对话，直接看 dense / sparse 命中了什么。"
                    actions={
                        <div className="flex items-center gap-2">
                            {/* admin 才有的选择：共享 or 个人。
                                一般用户没有这个开关——他们只能传个人文档，
                                给一个必然 403 的选项比不给更糟。 */}
                            {isAdmin ? (
                                <div className="seg-switch w-fit">
                                    <button
                                        data-active={uploadVisibility === "workspace"}
                                        onClick={() => setUploadVisibility("workspace")}
                                        title="全工作区可见，任何成员都能检索到"
                                    >
                                        <Users className="w-3.5 h-3.5 inline mr-1.5 -mt-0.5" />
                                        共享
                                    </button>
                                    <button
                                        data-active={uploadVisibility === "private"}
                                        onClick={() => setUploadVisibility("private")}
                                        title="只有你能检索到"
                                    >
                                        <Lock className="w-3.5 h-3.5 inline mr-1.5 -mt-0.5" />
                                        个人
                                    </button>
                                </div>
                            ) : (
                                <span className="px-3 py-2 rounded-xl bg-[#f3f0e6] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[11px] text-[#6e6b63] dark:text-[#a19f96] flex items-center gap-1.5">
                                    <Lock className="w-3.5 h-3.5" />
                                    上传为个人文档
                                </span>
                            )}
                            <button
                                onClick={handleUploadClick}
                                disabled={uploading}
                                className="btn-accent px-4 py-2.5 text-white text-xs font-medium rounded-xl flex items-center gap-2 disabled:opacity-60"
                            >
                                {uploading ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                    <Upload className="w-4 h-4" />
                                )}
                                {uploading ? "上传中..." : "上传文档"}
                            </button>
                        </div>
                    }
                />
                <input
                    ref={fileInputRef}
                    type="file"
                    className="hidden"
                    accept={fileTypes?.knowledgeAccept}
                    onChange={handleFileChange}
                />

                <div className="seg-switch w-fit">
                    <button
                        data-active={activeTab === "documents"}
                        onClick={() => setActiveTab("documents")}
                    >
                        <FileText className="w-3.5 h-3.5 inline mr-1.5 -mt-0.5" />
                        文档管理
                    </button>
                    <button
                        data-active={activeTab === "debug"}
                        onClick={() => setActiveTab("debug")}
                    >
                        <FlaskConical className="w-3.5 h-3.5 inline mr-1.5 -mt-0.5" />
                        检索调试
                    </button>
                </div>

                {activeTab === "documents" ? (
                    <div className="space-y-6">
                        {errorMsg && (
                            <div className="flex items-center justify-between gap-3 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs">
                                <span>{errorMsg}</span>
                                <button
                                    onClick={() => setErrorMsg(null)}
                                    className="p-0.5 hover:bg-rose-500/10 rounded"
                                >
                                    <X className="w-3.5 h-3.5" />
                                </button>
                            </div>
                        )}

                        {/* 拖拽区给所有人。此前只给 admin，而顶部那个上传按钮
                            对普通成员是可用的（传个人文档）——于是同一个页面上
                            按钮能用、拖拽不能，且旁边写着"文档由管理员维护"。
                            落点写在文案里，因为它对两种角色不一样。 */}
                        <div
                            className="drop-zone p-6 text-center anim-fade-up"
                            data-active={dragOver}
                            onDragOver={(e) => {
                                e.preventDefault();
                                setDragOver(true);
                            }}
                            onDragLeave={() => setDragOver(false)}
                            onDrop={handleDrop}
                            onClick={handleUploadClick}
                        >
                            <Upload className="w-5 h-5 text-[#da7756] mx-auto mb-2" />
                            <div className="text-xs font-medium text-[#1f1e1d] dark:text-[#edece8]">
                                拖入文档，或点击选择
                            </div>
                            <div className="text-[11px] text-[#918d83] mt-1">
                                txt / md / pdf / docx / xlsx / 代码文件 · 上传后后台切块并向量化
                            </div>
                            <div className="text-[11px] mt-1.5 flex items-center justify-center gap-1.5 text-[#6e6b63] dark:text-[#a19f96]">
                                {effectiveVisibility === "workspace" ? (
                                    <>
                                        <Users className="w-3 h-3" />
                                        存入 {workspace?.name ?? "工作区"} 共享库，全体成员可检索
                                    </>
                                ) : (
                                    <>
                                        <Lock className="w-3 h-3" />
                                        存为个人文档，只有你能检索到
                                    </>
                                )}
                            </div>
                        </div>

                        <div className="grid grid-cols-3 gap-4 anim-fade-up stagger-1">
                            <StatCard
                                label="文档总数"
                                value={loading ? "..." : String(totalDocs)}
                                icon={<FileText className="w-4 h-4" />}
                            />
                            <StatCard
                                label="向量分块"
                                value={loading ? "..." : String(totalChunks)}
                                accent
                                icon={<Layers className="w-4 h-4" />}
                                hint={
                                    !loading && unretrievableChunks > 0
                                        ? `其中 ${unretrievableChunks} 块属于成员个人文档，不进你的检索`
                                        : undefined
                                }
                            />
                            <StatCard
                                label="索引状态"
                                value={hasProcessing ? "处理中" : "就绪"}
                                ok={!hasProcessing}
                                icon={<ShieldCheck className="w-4 h-4" />}
                            />
                        </div>

                        {/* 加入工作区/重置邀请码之后要同时刷新工作区信息与文档列表：
                            换了空间，能看到的文档整批都变了 */}
                        <WorkspacePanel
                            workspace={workspace}
                            onChanged={() => {
                                apiClient
                                    .getWorkspace()
                                    .then(setWorkspace)
                                    .catch((e) =>
                                        toast.error(
                                            toastMessageFrom(e, "加载工作区信息失败"),
                                        ),
                                    );
                                refreshDocuments();
                            }}
                        />

                        <div className="card-surface rounded-2xl overflow-hidden anim-fade-up stagger-2">
                            <div className="p-4 border-b border-[#e6e2d8] dark:border-[#282724] flex items-center justify-between">
                                <div className="flex items-center gap-2 bg-[#f3f0e6] dark:bg-[#201f1c] px-3.5 py-2 rounded-xl border border-[#e3dfd5] dark:border-[#2e2d2a] w-72">
                                    <Search className="w-4 h-4 text-[#918d83]" />
                                    <input
                                        type="text"
                                        value={searchQuery}
                                        onChange={(e) => setSearchQuery(e.target.value)}
                                        placeholder="按文件名搜索文档..."
                                        className="bg-transparent border-none text-xs text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] focus:outline-none w-full"
                                    />
                                    {searchQuery && (
                                        <button
                                            onClick={() => setSearchQuery("")}
                                            className="text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
                                        >
                                            <X className="w-3.5 h-3.5" />
                                        </button>
                                    )}
                                </div>
                                <div className="flex items-center gap-3">
                                    {/* 归属筛选。"成员个人"那一档只有 admin 会有内容，
                                        所以 count 为 0 时不渲染——给一个永远空的筛选项
                                        比不给更让人困惑。 */}
                                    <div className="seg-switch w-fit">
                                        {(
                                            [
                                                ["all", "全部", counts.all],
                                                ["shared", "共享", counts.shared],
                                                ["mine", "我的个人", counts.mine],
                                                ["member", "成员个人", counts.member],
                                                ["inherited", "待处理", counts.inherited],
                                            ] as const
                                        )
                                            .filter(
                                                ([key, , count]) =>
                                                    key === "all" ||
                                                    key === "shared" ||
                                                    count > 0,
                                            )
                                            .map(([key, label, count]) => (
                                                <button
                                                    key={key}
                                                    data-active={scopeFilter === key}
                                                    onClick={() => setScopeFilter(key)}
                                                    title={
                                                        key === "member"
                                                            ? "成员的个人文档：你看得见（便于了解库里有什么），但不参与你的检索，也不能删除"
                                                            : key === "inherited"
                                                              ? "原上传者的账号已被删除，这些个人文档已转为共享，等你决定删或留"
                                                              : undefined
                                                    }
                                                >
                                                    {label}
                                                    <span className="ml-1 opacity-60">
                                                        {count}
                                                    </span>
                                                </button>
                                            ))}
                                    </div>
                                    <span className="text-xs text-[#6e6b63] dark:text-[#a19f96]">
                                        显示 {filteredDocs.length} / {totalDocs} 份文档
                                    </span>
                                </div>
                            </div>
                            {/* 只在 admin 主动切到这一档时解释一次。
                                这批文档的规则和其他两类都不一样（看得见、搜不到、删不掉），
                                而三条里有两条是"没有的东西"——不写出来只能靠试。 */}
                            {/* 这一批已经**全员可检索**了，所以横幅要说的是
                                "去看一眼"，而不是"这里有一批文档"。 */}
                            {scopeFilter === "inherited" && (
                                <div className="px-4 py-3 border-b border-[#e6e2d8] dark:border-[#282724] bg-sky-500/5 text-[11px] text-[#6e6b63] dark:text-[#a19f96] leading-relaxed">
                                    这些原本是个人文档，上传者的账号已被删除。留在原状态下
                                    谁都检索不到、也删不掉，所以已转为工作区共享——也就是说它们
                                    <span className="font-medium text-sky-700 dark:text-sky-400">
                                        现在全员可检索
                                    </span>
                                    。请确认内容适合团队共享，不合适就删掉。
                                </div>
                            )}
                            {scopeFilter === "member" && (
                                <div className="px-4 py-3 border-b border-[#e6e2d8] dark:border-[#282724] bg-amber-500/5 text-[11px] text-[#6e6b63] dark:text-[#a19f96] leading-relaxed">
                                    成员上传的个人文档。列在这里是为了让你知道工作区里存了什么
                                    （容量、合规），但它们
                                    <span className="font-medium text-amber-700 dark:text-amber-500">
                                        不参与你的检索
                                    </span>
                                    ，你也
                                    <span className="font-medium text-amber-700 dark:text-amber-500">
                                        不能删除
                                    </span>
                                    ——"个人文档只有我看得见"这个承诺是给上传者的。
                                    需要清理时请让文档主人自己删。
                                </div>
                            )}
                            <div className="divide-y divide-[#e6e2d8]/60 dark:divide-[#282724]/60">
                                {filteredDocs.map((doc) => (
                                    <div
                                        key={doc.id}
                                        className="p-4 flex items-center justify-between hover:bg-[#f3f0e6]/50 dark:hover:bg-[#22211e] transition-colors"
                                    >
                                        <div className="flex items-center gap-3 min-w-0">
                                            <div className="w-9 h-9 rounded-xl bg-[#da7756]/10 text-[#da7756] flex items-center justify-center shrink-0">
                                                <FileText className="w-4 h-4" />
                                            </div>
                                            <div className="min-w-0">
                                                <h4 className="text-xs font-medium text-[#1f1e1d] dark:text-[#edece8] truncate flex items-center gap-1.5">
                                                    {doc.name}
                                                    {/* 只给个人文档加标记：共享是这个页面的常态，
                                                        给常态加徽章只会让列表更难扫。
                                                        但"我的个人"和"别人的个人"必须分开——
                                                        后者我删不掉、也搜不到，混成一个徽章
                                                        就会以为删除按钮丢了。 */}
                                                    {doc.visibility === "private" &&
                                                        (classify(doc) === "mine" ? (
                                                            <span
                                                                className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[#f3f0e6] dark:bg-[#201f1c] text-[9px] text-[#6e6b63] dark:text-[#a19f96]"
                                                                title="个人文档，只有你能检索到"
                                                            >
                                                                <Lock className="w-2.5 h-2.5" />
                                                                个人
                                                            </span>
                                                        ) : (
                                                            <span
                                                                className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-amber-500/10 text-[9px] text-amber-700 dark:text-amber-500"
                                                                title="成员的个人文档：你看得见（便于了解库里存了什么），但它不参与你的检索，你也不能删除它"
                                                            >
                                                                <EyeOff className="w-2.5 h-2.5" />
                                                                成员个人
                                                            </span>
                                                        ))}
                                                    {/* 继承来的：共享，但需要一个决定。
                                                        和「成员个人」互斥（一个是私有一个是共享），
                                                        所以两个徽章不会同时出现。 */}
                                                    {doc.inherited && (
                                                        <span
                                                            className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-sky-500/10 text-[9px] text-sky-700 dark:text-sky-400"
                                                            title="原上传者的账号已被删除。这份个人文档已转为工作区共享，全员可检索——请确认内容是否适合团队共享，不合适就删掉"
                                                        >
                                                            <UserMinus className="w-2.5 h-2.5" />
                                                            继承
                                                        </span>
                                                    )}
                                                </h4>
                                                <span className="text-[10px] text-[#918d83]">
                                                    {formatSize(doc.size)}
                                                    {doc.createdAt
                                                        ? ` · ${new Date(doc.createdAt).toLocaleDateString()}`
                                                        : ""}
                                                    {/* 上传者只在不是自己时显示：看到一份删不掉的
                                                        文档时，"该找谁"是唯一可操作的信息 */}
                                                    {classify(doc) === "member" &&
                                                    doc.ownerName
                                                        ? ` · ${doc.ownerName}`
                                                        : ""}
                                                </span>
                                            </div>
                                        </div>
                                        <div className="flex items-center gap-4 text-xs shrink-0">
                                            <span className="text-[#6e6b63] dark:text-[#a19f96] font-mono">
                                                {doc.chunks} 块
                                            </span>
                                            <span
                                                className={`px-2.5 py-1 rounded-lg font-medium ${
                                                    doc.status === "indexed"
                                                        ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                                                        : doc.status === "processing"
                                                          ? "bg-amber-500/10 text-amber-600 dark:text-amber-400"
                                                          : "bg-rose-500/10 text-rose-600 dark:text-rose-400"
                                                }`}
                                            >
                                                {DOCUMENT_STATUS_LABELS[doc.status]}
                                            </span>
                                            {/* 按文档判，不按角色判：一般用户能删自己的个人文档，
                                                而 admin 删不了别人的个人文档。 */}
                                            {canDelete(doc) && (
                                                <button
                                                    onClick={() => handleDelete(doc.id)}
                                                    disabled={deletingId === doc.id}
                                                    title="删除文档"
                                                    aria-label={`删除文档 ${doc.name}`}
                                                    className="p-1.5 rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:text-rose-600 hover:bg-rose-500/10 transition-colors disabled:opacity-50"
                                                >
                                                    {deletingId === doc.id ? (
                                                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                                    ) : (
                                                        <Trash2 className="w-3.5 h-3.5" />
                                                    )}
                                                </button>
                                            )}
                                        </div>
                                    </div>
                                ))}
                                {filteredDocs.length === 0 && !loading && (
                                    <div className="p-14 text-center">
                                        <FileText className="w-8 h-8 text-[#dcd7cb] dark:text-[#33312d] mx-auto mb-3" />
                                        <p className="text-xs text-[#918d83]">
                                            {/* 三种"空"要分开说：筛没了、搜没了、真的空。
                                                都写成"库是空的"会让人以为文档丢了。 */}
                                            {searchQuery
                                                ? "未找到匹配的文档。"
                                                : scopeFilter !== "all"
                                                  ? "这一类下暂无文档，切到「全部」看看。"
                                                  : "库是空的。拖一份文档进来，下一轮对话就能引用。"}
                                        </p>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                ) : (
                    <RetrievalDebugger />
                )}
            </div>
        </div>
    );
};

