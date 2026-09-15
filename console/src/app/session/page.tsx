"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { SessionDetail } from "@/components/session-detail";

export default function SessionPage() {
  // useSearchParams requires a Suspense boundary under static export
  return (
    <Suspense>
      <SessionInner />
    </Suspense>
  );
}

function SessionInner() {
  // The session id lives in the query string: the export is static, so
  // sessions stay deep-linkable without a dynamic route segment.
  const sid = useSearchParams().get("sid");
  if (!sid) {
    return (
      <p className="pt-20 text-center text-[13px] text-muted-foreground">
        No session selected — pick one from the Sessions page.
      </p>
    );
  }
  return <SessionDetail sid={sid} />;
}
