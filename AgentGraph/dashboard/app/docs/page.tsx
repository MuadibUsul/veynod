import type { Metadata } from "next";

import { DocsView } from "./docs-view";
import { docsMetadata, docsPageJsonLd } from "./seo";

export const metadata: Metadata = docsMetadata({ slug: "introduction", lang: "en" });

export default function DocsHome() {
  const jsonLd = docsPageJsonLd({ slug: "introduction", lang: "en" });
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c"),
        }}
      />
      <DocsView slug="introduction" lang="en" />
    </>
  );
}
