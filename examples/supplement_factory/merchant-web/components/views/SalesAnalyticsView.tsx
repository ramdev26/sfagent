// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useMemo, useState } from "react";
import {
  formatDayMonth,
  formatMoney,
  formatNumber,
  Notice,
  PageHeader,
  Panel,
  Segmented,
  Skeleton,
  Sparkline,
  StatStrip,
  StatTile,
  useResource,
} from "web-shared";
import { fetchSalesAnalytics } from "@/lib/api";
import { formatCategoryLabel } from "@/lib/format";
import type { SalesPeriodId, SalesPeriodSummary, TopSellerRow } from "@/lib/types";

const PERIOD_OPTIONS: { id: SalesPeriodId; label: string }[] = [
  { id: "month", label: "This month" },
  { id: "prior_mtd", label: "Past month to date" },
  { id: "last_30_days", label: "Last 30 days" },
  { id: "last_month", label: "Last month" },
];

function barWidth(value: number, max: number): string {
  if (max <= 0 || value <= 0) return "0%";
  return `${Math.max(4, Math.round((value / max) * 100))}%`;
}

function DailyTable({ period, currency }: { period: SalesPeriodSummary; currency: string }) {
  const rows = [...period.daily].reverse();
  if (!rows.length) {
    return <p className="px-[18px] pb-4 text-[13.5px] text-(--ink-soft)">No daily totals for this period.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-(--line) text-left text-[12px] font-semibold text-(--ink-soft)">
            <th className="px-[18px] py-2.5">Day</th>
            <th className="px-3 py-2.5 text-right">Sales</th>
            <th className="px-3 py-2.5 text-right">Orders</th>
            <th className="px-3 py-2.5 text-right">Items</th>
            <th className="px-3 py-2.5 text-right">Shipping</th>
            <th className="py-2.5 pl-3 pr-[18px] text-right">Customers</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((day) => (
            <tr key={day.date} className="border-b border-(--line)/70 last:border-0">
              <td className="px-[18px] py-2 tabular-nums text-(--ink)">{formatDayMonth(day.date)}</td>
              <td className="px-3 py-2 text-right tabular-nums text-(--ink)">
                {formatMoney(day.sales, currency, { whole: day.sales >= 1000 })}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-(--ink)">{formatNumber(day.orders)}</td>
              <td className="px-3 py-2 text-right tabular-nums text-(--ink)">{formatNumber(day.items)}</td>
              <td className="px-3 py-2 text-right tabular-nums text-(--ink-soft)">
                {formatMoney(day.shipping, currency, { whole: true })}
              </td>
              <td className="py-2 pl-3 pr-[18px] text-right tabular-nums text-(--ink-soft)">
                {formatNumber(day.customers)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TopSellersTable({
  rows,
  currency,
}: {
  rows: TopSellerRow[];
  currency: string;
}) {
  if (!rows.length) {
    return <p className="px-[18px] pb-4 text-[13.5px] text-(--ink-soft)">No top sellers for this period.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-(--line) text-left text-[12px] font-semibold text-(--ink-soft)">
            <th className="px-[18px] py-2.5">#</th>
            <th className="px-3 py-2.5">Product</th>
            <th className="px-3 py-2.5 text-right">Qty</th>
            <th className="py-2.5 pl-3 pr-[18px] text-right">Est. revenue</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={row.listing_id || `${row.title}-${index}`} className="border-b border-(--line)/70 last:border-0">
              <td className="px-[18px] py-2.5 tabular-nums text-(--ink-soft)">{index + 1}</td>
              <td className="px-3 py-2.5">
                <div className="font-medium text-(--ink)">{row.title}</div>
                <div className="mt-0.5 text-[12px] text-(--ink-soft)">
                  {[row.category ? formatCategoryLabel(row.category) : null, row.listing_id ? `ID ${row.listing_id}` : null]
                    .filter(Boolean)
                    .join(" · ")}
                </div>
              </td>
              <td className="px-3 py-2.5 text-right tabular-nums font-medium text-(--ink)">{formatNumber(row.quantity)}</td>
              <td className="py-2.5 pl-3 pr-[18px] text-right tabular-nums text-(--ink)">
                {row.estimated_revenue != null
                  ? formatMoney(row.estimated_revenue, currency, { whole: row.estimated_revenue >= 1000 })
                  : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MixBars({
  rows,
  valueKey,
  labelKey,
  currency,
  money,
}: {
  rows: Record<string, string | number>[];
  valueKey: string;
  labelKey: string;
  currency: string;
  money?: boolean;
}) {
  if (!rows.length) {
    return <p className="px-[18px] pb-4 text-[13.5px] text-(--ink-soft)">No mix data yet.</p>;
  }
  const max = Math.max(...rows.map((row) => Number(row[valueKey]) || 0), 1);
  return (
    <ul className="flex flex-col gap-3 px-[18px] pb-4">
      {rows.map((row) => {
        const value = Number(row[valueKey]) || 0;
        const label = String(row[labelKey] ?? "");
        return (
          <li key={label}>
            <div className="mb-1 flex items-baseline justify-between gap-3 text-[13px]">
              <span className="min-w-0 truncate font-medium text-(--ink)">{label}</span>
              <span className="shrink-0 tabular-nums text-(--ink-soft)">
                {money ? formatMoney(value, currency, { whole: value >= 1000 }) : formatNumber(value)}
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-(--ground)">
              <div className="h-full rounded-full bg-(--ink)/70" style={{ width: barWidth(value, max) }} />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

export default function SalesAnalyticsView({
  refreshKey,
  onAskAssistant,
}: {
  refreshKey: number;
  onAskAssistant: (text: string) => void;
}) {
  const { data, failed } = useResource(fetchSalesAnalytics, [refreshKey]);
  const [periodId, setPeriodId] = useState<SalesPeriodId>("month");

  const period = data?.periods[periodId] ?? null;
  const sellers = data?.top_sellers[periodId] ?? [];
  const currency = data?.currency ?? "LKR";

  const salesSeries = useMemo(() => period?.daily.map((day) => day.sales) ?? [], [period]);
  const ordersSeries = useMemo(() => period?.daily.map((day) => day.orders) ?? [], [period]);

  const compareHint =
    periodId === "month"
      ? "past month to date"
      : periodId === "prior_mtd" || periodId === "last_30_days"
        ? "full last month"
        : undefined;

  return (
    <div className="ac-reveal flex flex-col gap-4">
      <PageHeader
        title="Sales analytics"
        subtitle={data ? data.note ?? "WooCommerce sales, orders, and product mix" : undefined}
      >
        {data ? (
          <Segmented<SalesPeriodId>
            label="Sales period"
            value={periodId}
            onChange={setPeriodId}
            options={PERIOD_OPTIONS}
          />
        ) : null}
      </PageHeader>

      {failed && !data ? (
        <Notice>The merchant API isn&apos;t reachable, so sales analytics can&apos;t load.</Notice>
      ) : !data || !period ? (
        <div className="grid gap-4">
          <Skeleton className="h-40" />
          <div className="grid gap-4 xl:grid-cols-2">
            <Skeleton className="h-80" />
            <Skeleton className="h-80" />
          </div>
        </div>
      ) : (
        <>
          <Panel
            title={period.label}
            subtitle={
              compareHint && period.sales_change_pct != null
                ? `vs ${compareHint}`
                : `${formatNumber(period.daily.length)} days with totals`
            }
            bodyClassName="pb-1"
          >
            <StatStrip>
              <StatTile
                label="Net sales"
                value={formatMoney(period.net_sales, currency, { whole: period.net_sales >= 1000 })}
                changePct={period.sales_change_pct}
                points={salesSeries}
                onClick={() =>
                  onAskAssistant(
                    `Break down ${period.label.toLowerCase()} net sales of ${formatMoney(period.net_sales, currency)}. What drove the result?`,
                  )
                }
                ariaLabel="Net sales: ask the assistant"
              />
              <StatTile
                label="Orders"
                value={formatNumber(period.orders)}
                changePct={period.orders_change_pct}
                points={ordersSeries}
                onClick={() =>
                  onAskAssistant(`How did order volume look for ${period.label.toLowerCase()}?`)
                }
                ariaLabel="Orders: ask the assistant"
              />
              <StatTile
                label="Average order"
                value={
                  period.average_order_value != null
                    ? formatMoney(period.average_order_value, currency)
                    : "—"
                }
                changePct={period.aov_change_pct}
                onClick={() =>
                  onAskAssistant(`What is moving average order value for ${period.label.toLowerCase()}?`)
                }
                ariaLabel="Average order: ask the assistant"
              />
              <StatTile
                label="Items sold"
                value={formatNumber(period.items)}
                changePct={period.items_change_pct}
                onClick={() =>
                  onAskAssistant(`Which products drove item volume in ${period.label.toLowerCase()}?`)
                }
                ariaLabel="Items sold: ask the assistant"
              />
            </StatStrip>
          </Panel>

          <div className="grid gap-4 xl:grid-cols-4">
            <Panel title="Gross sales" bodyClassName="px-[18px] pb-4 pt-1">
              <p className="text-[22px] font-semibold tabular-nums tracking-[-0.02em] text-(--ink)">
                {formatMoney(period.sales, currency, { whole: period.sales >= 1000 })}
              </p>
              <p className="mt-1 text-[12.5px] text-(--ink-soft)">Includes shipping; Woo “total sales”</p>
            </Panel>
            <Panel title="Shipping collected" bodyClassName="px-[18px] pb-4 pt-1">
              <p className="text-[22px] font-semibold tabular-nums tracking-[-0.02em] text-(--ink)">
                {formatMoney(period.shipping, currency, { whole: true })}
              </p>
              <p className="mt-1 text-[12.5px] text-(--ink-soft)">Delivery fees in the period</p>
            </Panel>
            <Panel title="Avg daily sales" bodyClassName="px-[18px] pb-4 pt-1">
              <p className="text-[22px] font-semibold tabular-nums tracking-[-0.02em] text-(--ink)">
                {formatMoney(period.average_daily_sales, currency, { whole: true })}
              </p>
              <p className="mt-1 text-[12.5px] text-(--ink-soft)">WooCommerce daily average</p>
            </Panel>
            <Panel title="Buyers (daily sum)" bodyClassName="px-[18px] pb-4 pt-1">
              <p className="text-[22px] font-semibold tabular-nums tracking-[-0.02em] text-(--ink)">
                {formatNumber(period.customers)}
              </p>
              <p className="mt-1 text-[12.5px] text-(--ink-soft)">May recount repeat customers</p>
            </Panel>
          </div>

          {(period.refunds > 0 || period.discount > 0 || period.tax > 0) && (
            <Panel title="Adjustments" bodyClassName="px-[18px] pb-4 pt-1">
              <div className="grid gap-3 sm:grid-cols-3 text-[13.5px]">
                <div>
                  <div className="text-(--ink-soft)">Refunds</div>
                  <div className="mt-0.5 font-semibold tabular-nums text-(--ink)">
                    {formatMoney(period.refunds, currency)}
                  </div>
                </div>
                <div>
                  <div className="text-(--ink-soft)">Discounts</div>
                  <div className="mt-0.5 font-semibold tabular-nums text-(--ink)">
                    {formatMoney(period.discount, currency)}
                  </div>
                </div>
                <div>
                  <div className="text-(--ink-soft)">Tax</div>
                  <div className="mt-0.5 font-semibold tabular-nums text-(--ink)">
                    {formatMoney(period.tax, currency)}
                  </div>
                </div>
              </div>
            </Panel>
          )}

          <Panel
            title="Sales trend"
            subtitle="Daily gross sales"
            bodyClassName="px-[18px] pb-4 pt-2"
          >
            {salesSeries.length > 1 ? (
              <Sparkline points={salesSeries} height={88} label={`${period.label} daily sales`} />
            ) : (
              <p className="text-[13.5px] text-(--ink-soft)">Not enough daily points to chart.</p>
            )}
          </Panel>

          <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
            <Panel title="Top sellers" subtitle={formatNumber(sellers.length)}>
              <TopSellersTable rows={sellers} currency={currency} />
            </Panel>
            <Panel title="Category mix" subtitle="From recent orders">
              <MixBars
                rows={data.mix.categories.map((row) => ({
                  category: formatCategoryLabel(row.category),
                  sales: row.sales,
                }))}
                labelKey="category"
                valueKey="sales"
                currency={currency}
                money
              />
            </Panel>
          </div>

          <div className="grid items-start gap-4 xl:grid-cols-2">
            <Panel title="Payment methods" subtitle="Recent paid orders">
              <MixBars
                rows={data.mix.payments.map((row) => ({
                  method: row.method,
                  sales: row.sales,
                }))}
                labelKey="method"
                valueKey="sales"
                currency={currency}
                money
              />
            </Panel>
            <Panel title="Order status (lifetime)" subtitle="All-time WooCommerce counts">
              <MixBars
                rows={data.order_status_lifetime.map((row) => ({
                  label: row.label || row.status,
                  orders: row.orders,
                }))}
                labelKey="label"
                valueKey="orders"
                currency={currency}
              />
            </Panel>
          </div>

          <Panel title="Daily breakdown" subtitle={`${formatNumber(period.daily.length)} days`}>
            <DailyTable period={period} currency={currency} />
          </Panel>
        </>
      )}
    </div>
  );
}
