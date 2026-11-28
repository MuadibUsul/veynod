import type { Metadata } from "next";

import { DocsView } from "../../../docs/docs-view";
import { docsMetadata, docsPageJsonLd } from "../../../docs/seo";
import { site } from "../../../docs/site";

export const dynamicParams = false;

export function generateStaticParams() {
  return Object.keys(site.pages)
    .filter((slug) => slug !== "introduction")
    .map((slug) => ({ slug }));
}

export function generateMetadata({ params }: { params: { slug: string } }): Metadata {
  return docsMetadata({ slug: params.slug, lang: "zh" });
}

export default function ZhDocsPage({ params }: { params: { slug: string } }) {
  const jsonLd = docsPageJsonLd({ slug: params.slug, lang: "zh" });
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c"),
        }}
      />
      <DocsView slug={params.slug} lang="zh" />
    </>
  );
}
