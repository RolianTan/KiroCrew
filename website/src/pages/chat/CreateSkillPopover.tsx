import { useState } from 'react'
import { Sparkles } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from '../../components/ui/popover'
import { Btn, Input } from '../../components/ui'
import { i18nT } from '../../i18n/t'

export default function CreateSkillPopover({ onSubmit }: { onSubmit: (purpose: string) => void }) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')

  // Purpose is mandatory: the button captures user intent, so an empty description
  // gives the authoring subagent nothing to disambiguate against the transcript.
  const canSubmit = draft.trim().length > 0

  const submit = () => {
    if (!canSubmit) return
    onSubmit(draft.trim())
    setDraft('')
    setOpen(false)
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="text-muted hover:text-text p-0.5 rounded transition-colors"
          title={i18nT('pages.chat.assistantMessage.create_skill')}
          aria-label={i18nT('pages.chat.assistantMessage.create_skill')}
        >
          <Sparkles size={14} />
        </button>
      </PopoverTrigger>
      <PopoverContent side="top" align="start" className="w-[320px] p-3 text-[12px]">
        <div className="text-muted text-[11px] mb-2 leading-relaxed">
          {i18nT('pages.chat.assistantMessage.create_skill_hint')}
        </div>
        {/*
          Password managers (1Password, LastPass, Bitwarden, Chrome autofill) decorate
          plain text inputs with an inline "import"/save affordance that visually reads
          as a second submit control. The data-* opt-outs suppress those injected UIs
          without altering the input's behaviour; autoComplete="off" backs them up for
          browsers that honour it. Refs: 1Password (data-1p-ignore), LastPass
          (data-lpignore), Bitwarden (data-form-type="other").
        */}
        <Input
          autoFocus
          required
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submit()
            }
          }}
          placeholder={i18nT('pages.chat.assistantMessage.create_skill_placeholder')}
          aria-label={i18nT('pages.chat.assistantMessage.create_skill_placeholder')}
          className="w-full mb-3"
          autoComplete="off"
          data-1p-ignore
          data-lpignore="true"
          data-form-type="other"
        />
        <div className="flex justify-end">
          <Btn primary onClick={submit} disabled={!canSubmit}>
            {i18nT('pages.chat.assistantMessage.create_skill_submit')}
          </Btn>
        </div>
      </PopoverContent>
    </Popover>
  )
}
