import type { Metadata } from "next";

import { DocsView } from "./docs-view";
import { docsMetadata, docsPageJsonLd } from "./seo";

const title = "Veynod Docs — Guides, MCP setup and API reference";
const description =
  "Guides, MCP setup and API reference for Veynod: a shared blackboard for agent teams with versioned memory, task leases and human approval.";

export const metadata: Metadata = docsMetadata({
  title,
  description,
  slug: "introduction",
  article: false,
});

export default function DocsHome() {
  const jsonLd = docsPageJsonLd({
    slug: "introduction",
    title: "Veynod Docs",
    description,
  });
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c"),
        }}
      />
      <DocsView slug="introduction" />
    </>
  );
}
