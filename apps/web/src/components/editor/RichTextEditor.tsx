"use client";

import { useEditor, EditorContent, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";

import {
  MergeToken,
  type MergeTokenDef,
  placeholdersToTokens,
  tokensToPlaceholders,
} from "./MergeToken";

const TOOLBAR_BTN =
  "rounded px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-200 data-[active=true]:bg-slate-300";

function ToolbarButton({
  editor,
  label,
  isActive,
  onClick,
}: {
  editor: Editor;
  label: string;
  isActive: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={TOOLBAR_BTN}
      data-active={isActive}
      // Keep editor selection on mousedown so formatting applies to it.
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      disabled={!editor.isEditable}
    >
      {label}
    </button>
  );
}

/**
 * Constrained rich-text editor for drip email bodies: headings, bold, italic,
 * links, lists, and merge-token chips only. The branded shell is code-managed
 * and never editable here.
 *
 * `value` and `onChange` speak the STORED form (HTML with literal `{{ token }}`
 * placeholders). The editor internally works in chip form; the transforms
 * bridge the two at the boundary.
 */
export function RichTextEditor({
  value,
  onChange,
  tokens,
}: {
  value: string;
  onChange: (html: string) => void;
  tokens: MergeTokenDef[];
}) {
  const editor = useEditor({
    immediatelyRender: false, // required under Next 15 to avoid hydration mismatch
    extensions: [
      StarterKit.configure({
        // Trim to the supported surface — no images, code blocks, quotes, etc.
        heading: { levels: [1, 2, 3] },
        codeBlock: false,
        blockquote: false,
        horizontalRule: false,
        strike: false,
      }),
      Link.configure({ openOnClick: false, autolink: true }),
      MergeToken,
    ],
    content: placeholdersToTokens(value, tokens),
    editorProps: {
      attributes: {
        class:
          "prose prose-sm max-w-none min-h-[10rem] rounded-b border border-t-0 border-slate-300 bg-white px-3 py-2 focus:outline-none",
      },
    },
    onUpdate: ({ editor: ed }) => {
      onChange(tokensToPlaceholders(ed.getHTML()));
    },
  });

  if (!editor) {
    return (
      <div className="min-h-[10rem] rounded border border-slate-300 bg-slate-50" />
    );
  }

  const insertToken = (t: MergeTokenDef) => {
    editor
      .chain()
      .focus()
      .insertContent({
        type: MergeToken.name,
        attrs: { token: t.name, label: t.label },
      })
      .run();
  };

  const setLink = () => {
    const prev = editor.getAttributes("link").href as string | undefined;
    const url = window.prompt("Link URL", prev ?? "https://");
    if (url === null) return;
    if (url === "") {
      editor.chain().focus().extendMarkRange("link").unsetLink().run();
      return;
    }
    editor.chain().focus().extendMarkRange("link").setLink({ href: url }).run();
  };

  return (
    <div>
      <div className="flex flex-wrap items-center gap-0.5 rounded-t border border-slate-300 bg-slate-100 px-1.5 py-1">
        <ToolbarButton
          editor={editor}
          label="H1"
          isActive={editor.isActive("heading", { level: 1 })}
          onClick={() =>
            editor.chain().focus().toggleHeading({ level: 1 }).run()
          }
        />
        <ToolbarButton
          editor={editor}
          label="H2"
          isActive={editor.isActive("heading", { level: 2 })}
          onClick={() =>
            editor.chain().focus().toggleHeading({ level: 2 }).run()
          }
        />
        <ToolbarButton
          editor={editor}
          label="H3"
          isActive={editor.isActive("heading", { level: 3 })}
          onClick={() =>
            editor.chain().focus().toggleHeading({ level: 3 }).run()
          }
        />
        <ToolbarButton
          editor={editor}
          label="Bold"
          isActive={editor.isActive("bold")}
          onClick={() => editor.chain().focus().toggleBold().run()}
        />
        <ToolbarButton
          editor={editor}
          label="Italic"
          isActive={editor.isActive("italic")}
          onClick={() => editor.chain().focus().toggleItalic().run()}
        />
        <ToolbarButton
          editor={editor}
          label="Link"
          isActive={editor.isActive("link")}
          onClick={setLink}
        />
        <ToolbarButton
          editor={editor}
          label="• List"
          isActive={editor.isActive("bulletList")}
          onClick={() => editor.chain().focus().toggleBulletList().run()}
        />
        <ToolbarButton
          editor={editor}
          label="1. List"
          isActive={editor.isActive("orderedList")}
          onClick={() => editor.chain().focus().toggleOrderedList().run()}
        />
        {tokens.length > 0 ? (
          <span className="ml-1 flex items-center gap-0.5 border-l border-slate-300 pl-1.5">
            <span className="text-xs text-slate-500">Insert:</span>
            {tokens.map((t) => (
              <button
                key={t.name}
                type="button"
                className={TOOLBAR_BTN}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => insertToken(t)}
              >
                {t.label}
              </button>
            ))}
          </span>
        ) : null}
      </div>
      <EditorContent editor={editor} />
    </div>
  );
}
