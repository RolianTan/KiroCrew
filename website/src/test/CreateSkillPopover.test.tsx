import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import CreateSkillPopover from '../pages/chat/CreateSkillPopover'

describe('CreateSkillPopover', () => {
  it('opens the prompt on click and submits the trimmed purpose', () => {
    const onSubmit = vi.fn()
    render(<CreateSkillPopover onSubmit={onSubmit} />)
    fireEvent.click(screen.getByTitle('Create skill'))
    const input = screen.getByPlaceholderText('Skill purpose')
    fireEvent.change(input, { target: { value: '  deploy runbook  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(onSubmit).toHaveBeenCalledWith('deploy runbook')
  })

  it('disables the submit button and no-ops when the purpose is empty or whitespace', () => {
    const onSubmit = vi.fn()
    render(<CreateSkillPopover onSubmit={onSubmit} />)
    fireEvent.click(screen.getByTitle('Create skill'))
    const submit = screen.getByRole('button', { name: 'Create' })
    // Empty: disabled and does not fire.
    expect(submit).toBeDisabled()
    fireEvent.click(submit)
    expect(onSubmit).not.toHaveBeenCalled()
    // Whitespace-only stays disabled — a description with no non-space characters
    // gives the authoring subagent nothing to disambiguate against the transcript.
    const input = screen.getByPlaceholderText('Skill purpose')
    fireEvent.change(input, { target: { value: '   ' } })
    expect(submit).toBeDisabled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('submits on Enter once a non-empty purpose is typed', () => {
    const onSubmit = vi.fn()
    render(<CreateSkillPopover onSubmit={onSubmit} />)
    fireEvent.click(screen.getByTitle('Create skill'))
    const input = screen.getByPlaceholderText('Skill purpose')
    fireEvent.change(input, { target: { value: 'a runbook' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSubmit).toHaveBeenCalledWith('a runbook')
  })

  it('opts the purpose input out of password-manager autofill decorations', () => {
    render(<CreateSkillPopover onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByTitle('Create skill'))
    const input = screen.getByPlaceholderText('Skill purpose') as HTMLInputElement
    // Browser password managers (1Password, LastPass, Bitwarden, Chrome autofill)
    // decorate plain inputs with an inline "import"/save button that reads as a
    // second submit control. These attributes tell each of them to skip the input.
    expect(input.getAttribute('autocomplete')).toBe('off')
    expect(input.getAttribute('data-1p-ignore')).not.toBeNull()
    expect(input.getAttribute('data-lpignore')).toBe('true')
    expect(input.getAttribute('data-form-type')).toBe('other')
  })
})
