/**
 * Editable code surface on Pierre's editor (`@pierre/diffs/edit`) — the
 * editing surface for every code-editing view. Lives beside `PierreImpl` in
 * the same lazy chunk; reach it through `../pierre` only.
 */
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef } from 'react'
import type { BaseCodeOptions, FileContents } from '@pierre/diffs'
import { EditProvider, File, MultiFileDiff } from '@pierre/diffs/react'
import { Editor, type EditorOptions } from '@pierre/diffs/edit'
import { useIsDark } from '../hooks/useIsDark'
import { pierreDiffOptions, pierreFileOptions, pierreThemeType } from './config'
import { contentCacheKey } from './PierreImpl'

export interface EditorMarker {
  severity: 'error' | 'warning' | 'info'
  message: string
  line: number
}

export interface PierreEditorHandle {
  /** Move the cursor to `line` (1-based), scroll it into view, and focus. */
  jumpToLine: (line: number) => void
  focus: () => void
}

function createEditor<LAnnotation>(options: EditorOptions<LAnnotation>) {
  return new Editor(options)
}

/** Approximate row scroll: Pierre lays lines out at --diffs-line-height. */
const LINE_HEIGHT_PX = 20

export const PierreEditorImpl = forwardRef<PierreEditorHandle, {
  file: FileContents
  options?: BaseCodeOptions
  onChange: (contents: string) => void
  /** Cmd/Ctrl+S inside the surface. */
  onSave?: () => void
  markers?: EditorMarker[]
  onCursorChange?: (line: number, column: number) => void
  /** Live-diff editing: the baseline contents to diff the edit session
   *  against (`null` = new file, whole buffer reads as added). `undefined`
   *  renders the plain editor. */
  diffBase?: string | null
  /** Split vs unified layout for the live-diff surface. */
  diffSplit?: boolean
  /** Show unchanged regions in the live-diff surface instead of folding them. */
  diffExpandUnchanged?: boolean
  className?: string
}>(function PierreEditorImpl({ file, options, onChange, onSave, markers, onCursorChange, diffBase, diffSplit, diffExpandUnchanged, className }, ref) {
  const dark = useIsDark()
  const resolved = useMemo(
    () => pierreFileOptions({ themeType: pierreThemeType(dark), ...options }),
    [dark, options],
  )
  const resolvedDiff = useMemo(
    () => pierreDiffOptions({
      themeType: pierreThemeType(dark),
      diffStyle: diffSplit ? 'split' : 'unified',
      ...(diffExpandUnchanged == null ? {} : { expandUnchanged: diffExpandUnchanged }),
      ...options,
    }),
    [dark, options, diffSplit, diffExpandUnchanged],
  )
  const baseFile = useMemo<FileContents | null>(
    () => (diffBase == null
      ? null
      : { name: file.name, contents: diffBase, cacheKey: contentCacheKey(file.name, diffBase) }),
    [diffBase, file.name],
  )
  const editorRef = useRef<Editor<undefined> | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const onSaveRef = useRef(onSave)
  onSaveRef.current = onSave
  const onCursorRef = useRef(onCursorChange)
  onCursorRef.current = onCursorChange

  const reportCursor = () => {
    const sel = editorRef.current?.getState()?.selections?.[0]
    if (sel) onCursorRef.current?.(sel.end.line + 1, sel.end.character + 1)
  }

  // Editor identity is per-mounted-surface: the factory caches by options
  // object identity, so this memo must be stable for the component lifetime.
  const editorOptions = useMemo<EditorOptions<undefined>>(
    () => ({
      onAttach(editor) {
        editorRef.current = editor
      },
      onChange(changed) {
        onChangeRef.current(changed.contents)
        reportCursor()
      },
    }),
    [],
  )

  useEffect(() => {
    const editor = editorRef.current
    if (!editor || markers == null) return
    editor.setMarkers(
      markers.map(m => ({
        severity: m.severity,
        message: m.message,
        start: { line: m.line - 1, character: 0 },
        end: { line: m.line - 1, character: Number.MAX_SAFE_INTEGER },
      })),
    )
  }, [markers])

  useImperativeHandle(ref, () => ({
    jumpToLine: (line: number) => {
      const editor = editorRef.current
      const zero = Math.max(0, line - 1)
      editor?.setSelections([{
        start: { line: zero, character: 0 },
        end: { line: zero, character: 0 },
        direction: 'none',
      }])
      editor?.focus()
      const scroller = containerRef.current
      if (scroller) scroller.scrollTop = Math.max(0, zero * LINE_HEIGHT_PX - scroller.clientHeight / 2)
    },
    focus: () => editorRef.current?.focus(),
  }), [])

  return (
    // The wrapper only intercepts the save chord and mirrors cursor position;
    // the interactive, focusable surface is Pierre's own editable content
    // inside — a role here would misdescribe the scroll container.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      ref={containerRef}
      className={`pierre-surface h-full w-full overflow-auto ${className ?? ''}`}
      onKeyDownCapture={e => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
          e.preventDefault()
          onSaveRef.current?.()
        }
      }}
      onKeyUp={reportCursor}
      onMouseUp={reportCursor}
    >
      <EditProvider createEditor={createEditor}>
        {diffBase !== undefined ? (
          // Live-diff edit session: Pierre diffs the buffer against the
          // baseline as you type. Keyed so flipping modes rebuilds the edit
          // session rather than rebinding one editor across surface kinds.
          <MultiFileDiff
            key="diff"
            oldFile={baseFile}
            newFile={file}
            edit
            editorOptions={editorOptions}
            options={resolvedDiff}
          />
        ) : (
          <File key="file" file={file} edit editorOptions={editorOptions} options={resolved} />
        )}
      </EditProvider>
    </div>
  )
})
