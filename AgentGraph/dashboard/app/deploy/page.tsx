"use client";

import { useEffect } from "react";

/**
 * The v1 deploy gate page was retired on 2026-11-05. Deploys live in
 * Settings > Deploys (deploy gate v2, docs/DEPLOYS.md); old links land there.
 */
export default function DeployRedirect() {
  useEffect(() => {
    window.location.replace("/dashboard/#deploys");
  }, []);
  return null;
}
