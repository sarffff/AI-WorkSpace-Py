"""业务靶子系统：客户、订单、物流、退款、发票，以及写操作账本。

见 models 里 Cs* 那一组。真实部署里这些是 ERP / CRM / 支付网关 / 物流平台，
Agent 只能通过工具读写；这里自建同名结构，是为了让"改地址、发起退款"有对象可写，
并在离线评估里能断言**到底改没改对**——否则工单解决率只是在测模型会不会说话。

cs_operations 上的 idempotency_key 唯一约束是幂等唯一的硬保证：模型重试、进程崩溃后
恢复、用户重复点击，撞的都是同一个键，撞了就返回首次结果而不是再退一笔。
cs_refunds 只记状态、不动钱；executed 在真实系统里由支付网关回调写。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0022_cs_business_system"
down_revision: Union[str, None] = "0021_ticket_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cs_customers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        # 渠道侧客户标识。不做唯一约束：同一人从两个渠道进来正是要合并画像的场景
        sa.Column("external_ref", sa.String(120), nullable=True),
        # 存归一化之后的形式（小写、只留数字），否则同一个人会因写了 +86 裂成两行
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("display_name", sa.String(120), nullable=True),
        sa.Column("tier", sa.String(20), nullable=False, server_default="standard"),
        sa.Column("lifetime_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        # 投诉史/法律函件等标记，json 数组。风险分级要读它
        sa.Column("risk_flags", sa.Text(), nullable=True),
        # 长期画像，json 对象
        sa.Column("profile", sa.Text(), nullable=True),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cs_customers_ws_email", "cs_customers", ["workspace_id", "email"])
    op.create_index("ix_cs_customers_ws_phone", "cs_customers", ["workspace_id", "phone"])
    op.create_index(
        "ix_cs_customers_ws_external", "cs_customers", ["workspace_id", "external_ref"]
    )

    op.create_table(
        "cs_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=True),
        # 客户口中报出来的那个号，工具按它查，所以工作区内必须唯一
        sa.Column("order_no", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="paid"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        # 单独一列是超额退款第二道保险（幂等键之外的兜底）
        sa.Column("refunded_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("receiver_name", sa.String(60), nullable=True),
        sa.Column("receiver_phone", sa.String(32), nullable=True),
        sa.Column("address_text", sa.String(500), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        # 唯一约束写在建表里（SQLite 不支持 ALTER 加约束，同 0013/0014）
        sa.UniqueConstraint("workspace_id", "order_no", name="uq_cs_orders_ws_orderno"),
    )
    op.create_index("ix_cs_orders_customer", "cs_orders", ["customer_id"])
    op.create_index("ix_cs_orders_ws_created", "cs_orders", ["workspace_id", "created_at"])

    op.create_table(
        "cs_order_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        # NULL = 跟随订单头
        sa.Column("line_status", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cs_order_items_order", "cs_order_items", ["order_id"])

    op.create_table(
        "cs_shipments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("carrier", sa.String(40), nullable=False),
        sa.Column("tracking_no", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="in_transit"),
        sa.Column("last_event", sa.String(500), nullable=True),
        sa.Column("last_event_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cs_shipments_order", "cs_shipments", ["order_id"])
    op.create_index("ix_cs_shipments_tracking", "cs_shipments", ["tracking_no"])

    op.create_table(
        "cs_refunds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("ticket_id", sa.String(36), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
        # requested / awaiting_approval / approved / executed / rejected / failed
        sa.Column("status", sa.String(20), nullable=False, server_default="requested"),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(36), nullable=True),
        # 人审痕迹：谁批的、批的是不是改过的参数
        sa.Column("approved_by", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        # 幂等是数据库事实，不是应用约定
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_cs_refunds_ws_idem"
        ),
    )
    op.create_index("ix_cs_refunds_order", "cs_refunds", ["order_id"])
    op.create_index("ix_cs_refunds_customer", "cs_refunds", ["customer_id"])
    op.create_index(
        "ix_cs_refunds_ws_status_created",
        "cs_refunds",
        ["workspace_id", "status", "created_at"],
    )

    op.create_table(
        "cs_invoices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("ticket_id", sa.String(36), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        # 个人抬头的发票就没有税号，所以可空
        sa.Column("tax_no", sa.String(40), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        # requested / issued / rejected。issued 由财务系统回填，本仓库的工具只到 requested
        sa.Column("status", sa.String(16), nullable=False, server_default="requested"),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cs_invoices_order", "cs_invoices", ["order_id"])
    op.create_index("ix_cs_invoices_ws_status", "cs_invoices", ["workspace_id", "status"])

    op.create_table(
        "cs_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("ticket_id", sa.String(36), nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(40), nullable=False),
        # read / mutate / fund。权限档位记在账上，事后才能证明这一笔当时是高权限
        sa.Column("permission", sa.String(8), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column("args_digest", sa.String(64), nullable=True),
        sa.Column("args_preview", sa.Text(), nullable=True),
        sa.Column("result_excerpt", sa.Text(), nullable=True),
        # executed / replayed / blocked / failed / pending_approval
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("blocked_reason", sa.String(200), nullable=True),
        # 非资金类为 NULL 而不是 0——0 会被算进当日额度
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("actor", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_cs_operations_ws_idem"
        ),
    )
    op.create_index("ix_cs_operations_ticket", "cs_operations", ["ticket_id"])
    op.create_index("ix_cs_operations_ws_created", "cs_operations", ["workspace_id", "created_at"])
    # 当日资金额度聚合
    op.create_index(
        "ix_cs_operations_ws_kind_time",
        "cs_operations",
        ["workspace_id", "permission", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cs_operations_ws_kind_time", table_name="cs_operations")
    op.drop_index("ix_cs_operations_ws_created", table_name="cs_operations")
    op.drop_index("ix_cs_operations_ticket", table_name="cs_operations")
    op.drop_table("cs_operations")

    op.drop_index("ix_cs_invoices_ws_status", table_name="cs_invoices")
    op.drop_index("ix_cs_invoices_order", table_name="cs_invoices")
    op.drop_table("cs_invoices")

    op.drop_index("ix_cs_refunds_ws_status_created", table_name="cs_refunds")
    op.drop_index("ix_cs_refunds_customer", table_name="cs_refunds")
    op.drop_index("ix_cs_refunds_order", table_name="cs_refunds")
    op.drop_table("cs_refunds")

    op.drop_index("ix_cs_shipments_tracking", table_name="cs_shipments")
    op.drop_index("ix_cs_shipments_order", table_name="cs_shipments")
    op.drop_table("cs_shipments")

    op.drop_index("ix_cs_order_items_order", table_name="cs_order_items")
    op.drop_table("cs_order_items")

    op.drop_index("ix_cs_orders_ws_created", table_name="cs_orders")
    op.drop_index("ix_cs_orders_customer", table_name="cs_orders")
    op.drop_table("cs_orders")

    op.drop_index("ix_cs_customers_ws_external", table_name="cs_customers")
    op.drop_index("ix_cs_customers_ws_phone", table_name="cs_customers")
    op.drop_index("ix_cs_customers_ws_email", table_name="cs_customers")
    op.drop_table("cs_customers")
