// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AssistantRail,
  Inspector,
  Notice,
  PageHeader,
  type PortalNavItem,
  PortalShell,
  type Prefill,
  Skeleton,
  useMerchantChat,
  useResource,
  useSession,
} from "web-shared";
import AssistantPanel from "@/components/AssistantPanel";
import CatalogView from "@/components/views/CatalogView";
import HomeView from "@/components/views/HomeView";
import InventoryView from "@/components/views/InventoryView";
import OrdersView from "@/components/views/OrdersView";
import { api, fetchOverview, UNREACHABLE } from "@/lib/api";
import type { StagedChange } from "@/lib/types";

type PortalView = "home" | "catalog" | "orders" | "inventory";

function StoreMark() {
  return (
    <span
      aria-hidden
      className="grid h-[34px] w-[34px] shrink-0 place-items-center rounded-[10px] bg-(--ink) text-[13px] font-bold text-(--surface) shadow-[inset_0_-3px_0_rgba(0,0,0,0.18)]"
    >
      SF
    </span>
  );
}

export default function PortalPage() {
  const session = useSession(api);
  const [view, setView] = useState<PortalView>("home");
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [prefill, setPrefill] = useState<Prefill | null>(null);
  // Bumped whenever a staged change moves, so every widget re-reads the store the agent wrote.
  const [refreshKey, setRefreshKey] = useState(0);
  const refreshPortal = useCallback(() => setRefreshKey((value) => value + 1), []);

  const chat = useMerchantChat<StagedChange>(api, {
    ...session,
    unreachable: UNREACHABLE,
    onPortalRefresh: refreshPortal,
  });

  // The overview feeds the home page and the sidebar counts, so it loads here.
  const { data: overview, failed: overviewFailed } = useResource(session.sessionId ? fetchOverview : null, [session.sessionId, refreshKey]);

  // The rail is part of the default layout on wide screens; narrow screens open it on demand.
  useEffect(() => {
    const media = window.matchMedia("(min-width: 1024px)");
    const sync = () => setAssistantOpen(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  // Session start is silent until it fails; surface a timeout so the main pane is never blank.
  const [sessionTimedOut, setSessionTimedOut] = useState(false);
  useEffect(() => {
    if (session.sessionId) {
      setSessionTimedOut(false);
      return;
    }
    const timer = window.setTimeout(() => setSessionTimedOut(true), 8000);
    return () => window.clearTimeout(timer);
  }, [session.sessionId]);

  const askAssistant = useCallback((text: string) => {
    setAssistantOpen(true);
    setPrefill({ text, nonce: Date.now() });
  }, []);

  const nav = useMemo<PortalNavItem<PortalView>[]>(() => {
    const alerts = overview?.snapshot.alerts;
    return [
      { id: "home", label: "Home", icon: "home" },
      { id: "catalog", label: "Catalog", icon: "tag" },
      { id: "orders", label: "Orders", icon: "inbox", attention: alerts?.order_issues || null },
      {
        id: "inventory",
        label: "Inventory",
        icon: "box",
        count: alerts ? (alerts.low_stock ?? 0) + (alerts.slow_movers ?? 0) : null,
      },
    ];
  }, [overview]);

  return (
    <>
      <PortalShell
        brand={{ mark: <StoreMark />, name: "Supplement Factory", detail: "Merchant workspace" }}
        nav={nav}
        view={view}
        onViewChange={setView}
        operator={{ name: session.operator ?? "Staff", role: "Store manager" }}
        assistantOpen={assistantOpen}
        assistantBusy={chat.busy}
        onToggleAssistant={() => setAssistantOpen((open) => !open)}
        rail={
          <AssistantRail
            open={assistantOpen}
            storageKey="supplement-factory-merchant-panel-width"
            onClose={() => setAssistantOpen(false)}
          >
            {(rail) => (
              <AssistantPanel
                chat={chat}
                prefill={prefill}
                onPrefill={askAssistant}
                newMemoryCount={chat.newMemoryKeys.size}
                onOpenActivity={() => setActivityOpen(true)}
                {...rail}
              />
            )}
          </AssistantRail>
        }
      >
        {session.sessionId ? (
          <>
            {view === "home" ? (
              <HomeView
                data={overview}
                failed={overviewFailed}
                operator={session.operator}
                onAskAssistant={askAssistant}
                onNavigate={setView}
              />
            ) : null}
            {view === "catalog" ? <CatalogView refreshKey={refreshKey} onAskAssistant={askAssistant} /> : null}
            {view === "orders" ? (
              <OrdersView refreshKey={refreshKey} recentOrders={overview?.recent_orders ?? (overviewFailed ? [] : null)} onAskAssistant={askAssistant} />
            ) : null}
            {view === "inventory" ? <InventoryView refreshKey={refreshKey} onAskAssistant={askAssistant} /> : null}
          </>
        ) : sessionTimedOut ? (
          <>
            <PageHeader title="Merchant workspace" subtitle="Waiting for API" />
            <Notice>{UNREACHABLE}</Notice>
          </>
        ) : (
          <>
            <PageHeader title="Merchant workspace" subtitle="Starting session…" />
            <Skeleton className="h-36" />
            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_300px]">
              <Skeleton className="h-96" />
              <Skeleton className="h-72" />
            </div>
          </>
        )}
      </PortalShell>
      {activityOpen ? (
        <Inspector
          turnCount={chat.turnCount}
          streaming={chat.streaming}
          trace={chat.trace}
          memory={chat.memory}
          newMemoryKeys={chat.newMemoryKeys}
          memoryTitle="Business memory"
          onClose={() => setActivityOpen(false)}
        />
      ) : null}
    </>
  );
}
