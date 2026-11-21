"use client";

import Link from "next/link";
import { getApi } from "@/lib/backend";
import { EmptyState, ErrorNotice, PageHead, Skeleton, useLoader } from "../_ui";

export default function TemplatesPage() {
  const templates = useLoader(() => getApi().listTemplates(), []);
  return (
    <div className="mx-auto max-w-5xl">
      <PageHead
        eyebrow="Templates"
        title="Start from a proven specialist"
        lede="Each template is a ready-made agent definition. Using one creates your own copy that you can edit; the template itself never changes."
      />
      <div className="mt-6">
        {templates.loading ? (
          <Skeleton lines={4} height="5rem" />
        ) : templates.error ? (
          <ErrorNotice error={templates.error} onRetry={templates.reload} />
        ) : templates.data!.items.length === 0 ? (
          <EmptyState title="No templates available">You can still create an agent from scratch.</EmptyState>
        ) : (
          <div>
            {templates.data!.items.map((t) => (
              <Link key={t.id} href={`/studio/templates/${t.id}`} className="st-row" style={{ gridTemplateColumns: "minmax(0,1fr) auto" }}>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-baseline gap-2">
                    <span className="st-row-title">{t.name}</span>
                    <span className="st-muted text-sm">{t.role}</span>
                  </div>
                  <p className="st-muted mt-1 text-sm leading-relaxed">{t.description}</p>
                  {t.capabilities.length ? (
                    <div className="st-task-meta">
                      {t.capabilities.slice(0, 6).map((c) => (
                        <span key={c} className="st-chip">
                          {c}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
                <span className="st-chip st-mono">v{t.version}</span>
              </Link>
            ))}
          </div>
        )}
        <div className="mt-6">
          <Link href="/studio/agents/new" className="st-btn">
            Or create an agent from scratch
          </Link>
        </div>
      </div>
    </div>
  );
}
