import Uploader from "@/components/Uploader";

export default function Home() {
  return (
    <div className="space-y-10">
      <div>
        <h1 className="text-3xl font-bold tracking-tight text-slate-900">
          Humanize your document, layout untouched.
        </h1>
        <p className="mt-3 max-w-2xl text-slate-600">
          Upload a .docx and get back the same document with body text
          rewritten to read naturally — images, tables, and formatting stay
          exactly where they are. References, citations, and scientific
          notation are automatically detected and left untouched, and the
          rewritten text always preserves the original meaning.
        </p>
      </div>
      <Uploader />
    </div>
  );
}
