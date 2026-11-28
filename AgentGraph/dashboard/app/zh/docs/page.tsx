import type { Metadata } from "next";

import { DocsView } from "../../docs/docs-view";
import { docsMetadata, docsPageJsonLd } from "../../docs/seo";

export const metadata: Metadata = docsMetadata({ slug: "introduction", lang: "zh" });

export default function ZhDocsHome() {
  const jsonLd = docsPageJsonLd({ slug: "introduction", lang: "zh" });
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c"),
        }}
      />
      <DocsView slug="introduction" lang="zh" />
    </>
  );
}
