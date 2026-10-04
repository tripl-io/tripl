import { createRef, useState } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { INPUT_BASE, INPUT_EDGE } from './input-style'
import {
  Field,
  NativeSelect,
  PageHead,
  Panel,
  RadioCards,
  SCard,
  SettingsSaveBar,
  SHeader,
  TextArea,
  TextInput,
  ToggleRow,
} from './kit'

describe('Panel header', () => {
  /**
   * The header clipping is pure layout, so the unit-level guard is
   * the class contract that produces it: a wrapping header whose right slot is
   * allowed to shrink, and a title that keeps a basis so it cannot collapse to
   * 0px behind the controls. Measured widths are covered by the browser pass.
   */
  function header(container: HTMLElement): HTMLElement {
    const found = container.querySelector('header')
    expect(found).not.toBeNull()
    return found as HTMLElement
  }

  it('wraps its controls instead of pinning them past the card edge', () => {
    const { container } = render(
      <Panel title="Catalog" subtitle="1 total" right={<button type="button">All kinds</button>}>
        <div>rows</div>
      </Panel>,
    )

    expect(header(container).className).toContain('flex-wrap')

    const rightSlot = header(container).lastElementChild as HTMLElement
    expect(rightSlot.textContent).toBe('All kinds')
    expect(rightSlot.className).not.toContain('shrink-0')
    expect(rightSlot.className).toContain('flex-wrap')
  })

  it('gives the title a basis so it cannot be crushed to zero width', () => {
    const { container } = render(
      <Panel title="Catalog" right={<button type="button">All kinds</button>}>
        <div>rows</div>
      </Panel>,
    )

    const titleColumn = header(container).firstElementChild as HTMLElement
    expect(titleColumn.textContent).toBe('Catalog')
    expect(titleColumn.className).toContain('basis-40')
  })
})

describe('Field label association', () => {
  /**
   * Field generated an id for its <label htmlFor> but rendered its children
   * raw, so unless a caller passed `htmlFor` AND repeated the id on its own
   * control the label addressed nothing — 10 of 14 inputs on
   * /settings/instance/ai had no accessible name.
   */
  it('names an input, a textarea and a select the caller gave no id', () => {
    const noop = () => {}
    render(
      <>
        <Field label="Base URL">
          <TextInput value="https://example.com" onChange={noop} />
        </Field>
        <Field label="Ask prompt" stacked>
          <TextArea value="You are..." onChange={noop} />
        </Field>
        <Field label="Log level" last>
          <NativeSelect value="INFO" onChange={noop} options={['INFO', 'DEBUG']} />
        </Field>
      </>,
    )

    expect(screen.getByLabelText('Base URL')).toHaveValue('https://example.com')
    expect(screen.getByLabelText('Ask prompt')).toHaveValue('You are...')
    expect(screen.getByLabelText('Log level')).toHaveValue('INFO')
  })

  it('reaches a control nested below the immediate child', () => {
    // The secret fields wrap their input next to a "Clear" button, so the id
    // cannot be injected into the immediate child element.
    render(
      <Field label="AI API key">
        <div className="flex gap-2">
          <TextInput type="password" value="" onChange={() => {}} />
          <button type="button">Clear</button>
        </div>
      </Field>,
    )

    expect(screen.getByLabelText('AI API key')).toHaveAttribute('type', 'password')
  })

  it('gives the row id to one control only when a Field wraps several', () => {
    // The metric form's "Filters" row wraps a filter editor that renders two
    // Selects and a TextInput per condition. Handing all three the Field's id
    // put duplicate ids on focusable elements and left the label resolving to
    // whichever happened to come first in the DOM.
    const { container } = render(
      <Field label="Filters" stacked last>
        <NativeSelect value="a" onChange={() => {}} options={['a']} aria-label="Column" />
        <NativeSelect value="=" onChange={() => {}} options={['=']} aria-label="Operator" />
        <TextInput value="" onChange={() => {}} aria-label="Value" />
      </Field>,
    )

    const target = (container.querySelector('label') as HTMLLabelElement).htmlFor
    const ids = Array.from(container.querySelectorAll('[id]')).map((el) => el.id)
    expect(ids.filter((id) => id === target)).toHaveLength(1)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('names a row that holds no control as a group instead of dangling its label', () => {
    // "Avatar" (two buttons), "Role" (a chip) and "Connection" (a test button)
    // have nothing a <label> can point at, so they opt out and name a group.
    const { container } = render(
      <Field label="Role" htmlFor={false} last>
        <span>Owner</span>
      </Field>,
    )

    expect(container.querySelector('label')).toBeNull()
    expect(screen.getByRole('group', { name: 'Role' })).toBeInTheDocument()
  })

  it('leaves an explicitly passed id alone', () => {
    render(
      <Field label="Self-service registration" htmlFor="security-registration-mode">
        <NativeSelect
          id="security-registration-mode"
          value="open"
          onChange={() => {}}
          options={['open', 'disabled']}
        />
      </Field>,
    )

    expect(screen.getByLabelText('Self-service registration')).toHaveAttribute(
      'id',
      'security-registration-mode',
    )
  })
})

describe('SCard header', () => {
  it('titles itself at h2 so settings pages read h1 → h2', () => {
    render(
      <SCard title="Project details" description="Identity and configuration.">
        <div>body</div>
      </SCard>,
    )

    expect(screen.getByRole('heading', { name: 'Project details', level: 2 })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 3 })).toBeNull()
  })
})

describe('TextInput — form attributes', () => {
  it('forwards type, required, readOnly, list, autoComplete and ref to the input', () => {
    const ref = createRef<HTMLInputElement>()
    render(
      <TextInput
        ref={ref}
        type="email"
        required
        readOnly
        list="suggestions"
        autoComplete="off"
        value="a@example.com"
        aria-label="Email"
      />,
    )

    // A `list` makes it a combobox: the datalist offers suggestions.
    const input = screen.getByRole('combobox', { name: 'Email' })
    expect(input).toHaveAttribute('type', 'email')
    expect(input).toBeRequired()
    expect(input).toHaveAttribute('readonly')
    expect(input).toHaveAttribute('list', 'suggestions')
    expect(input).toHaveAttribute('autocomplete', 'off')
    expect(ref.current).toBe(input)
  })
})

describe('Kit control contrast', () => {
  // --border measures 1.20-1.31:1 against the surfaces; the form-control edge
  // must be --input, the token theme-contrast.test.ts pins to 3:1.
  // Pinned on the shared style objects: jsdom's CSS parser is not a reliable
  // witness for a `border` shorthand holding var().
  it('draws text fields, selects and textareas on the --input edge', () => {
    expect(INPUT_EDGE).toBe('1px solid var(--input)')
    expect(INPUT_BASE.border).toBe(INPUT_EDGE)
  })

  it('paints an off Toggle with the --input token, not --border-strong', () => {
    render(<ToggleRow label="Enabled" value={false} />)
    const toggle = screen.getByRole('switch', { name: 'Enabled' })
    expect(toggle).toHaveStyle({ background: 'var(--input)' })
  })
})

describe('Field required and error', () => {
  it('marks the control required without putting the asterisk in its name', () => {
    render(
      <Field label="Name" required>
        <TextInput value="" />
      </Field>,
    )
    const input = screen.getByRole('textbox', { name: 'Name' })
    expect(input).toHaveAttribute('aria-required', 'true')
  })

  it('ties the error to the control and announces it', () => {
    render(
      <Field label="Slug" error="Use lowercase letters only.">
        <TextInput value="Bad Slug" />
      </Field>,
    )
    const input = screen.getByRole('textbox', { name: 'Slug' })
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('Use lowercase letters only.')
    expect(screen.getByRole('alert')).toHaveTextContent('Use lowercase letters only.')
  })

  it('lets a control keep its own aria wiring', () => {
    render(
      <>
        <p id="own">Own hint</p>
        <Field label="Region" error="Pick one.">
          <NativeSelect value="eu" options={['eu', 'us']} aria-describedby="own" />
        </Field>
      </>,
    )
    const select = screen.getByRole('combobox', { name: 'Region' })
    expect(select).toHaveAccessibleDescription('Own hint')
    expect(select).toHaveAttribute('aria-invalid', 'true')
  })

  it('shows no error and no invalid state without one', () => {
    render(
      <Field label="Name">
        <TextInput value="" />
      </Field>,
    )
    expect(screen.getByRole('textbox', { name: 'Name' })).not.toHaveAttribute('aria-invalid')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('Section headings', () => {
  it('names a Panel section by a real heading', () => {
    render(
      <Panel title="Data match">
        <div>rows</div>
      </Panel>,
    )
    expect(screen.getByRole('heading', { level: 2, name: 'Data match' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Data match' })).toBeInTheDocument()
  })

  it('renders no empty heading for an SCard with only a description', () => {
    render(<SCard description="Only a description." />)
    expect(screen.queryByRole('heading')).toBeNull()
  })

  it('takes a heading level for a nested SCard', () => {
    render(<SCard title="Nested" headingLevel={3} />)
    expect(screen.getByRole('heading', { level: 3, name: 'Nested' })).toBeInTheDocument()
  })
})

describe('RadioCards keyboard', () => {
  function Harness() {
    const [value, setValue] = useState('a')
    return (
      <RadioCards
        groupLabel="Case style"
        value={value}
        onChange={setValue}
        options={[
          { value: 'a', label: 'Alpha' },
          { value: 'b', label: 'Beta' },
          { value: 'c', label: 'Gamma' },
        ]}
      />
    )
  }

  it('is one Tab stop and moves the choice with the arrow keys', () => {
    render(<Harness />)
    const [alpha, beta, gamma] = screen.getAllByRole('radio')
    expect(alpha).toHaveAttribute('tabindex', '0')
    expect(beta).toHaveAttribute('tabindex', '-1')
    expect(gamma).toHaveAttribute('tabindex', '-1')

    fireEvent.keyDown(alpha!, { key: 'ArrowRight' })
    expect(screen.getByRole('radio', { name: 'Beta' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Beta' })).toHaveFocus()

    fireEvent.keyDown(screen.getByRole('radio', { name: 'Beta' }), { key: 'ArrowLeft' })
    fireEvent.keyDown(screen.getByRole('radio', { name: 'Alpha' }), { key: 'ArrowUp' })
    // Wraps from the first to the last.
    expect(screen.getByRole('radio', { name: 'Gamma' })).toBeChecked()
  })
})

describe('RadioCards dimmed option', () => {
  function Harness() {
    const [value, setValue] = useState('a')
    return (
      <RadioCards
        groupLabel="Metric kind"
        value={value}
        onChange={setValue}
        options={[
          { value: 'a', label: 'Alpha' },
          { value: 'b', label: 'Beta', dimmed: true },
        ]}
      />
    )
  }

  it('fades a card without disabling it, and not once it is checked', () => {
    render(<Harness />)
    const beta = screen.getByRole('radio', { name: 'Beta' })
    expect(beta).toHaveAttribute('data-dimmed', 'true')
    expect(beta).toBeEnabled()

    fireEvent.click(beta)
    expect(beta).toBeChecked()
    expect(beta).not.toHaveAttribute('data-dimmed')
  })
})

describe('Panel options', () => {
  it('drops the header and its heading when there is neither title nor right slot', () => {
    const { container } = render(<Panel>body</Panel>)

    expect(screen.queryByRole('heading')).toBeNull()
    expect(container.querySelector('header')).toBeNull()
    expect(container.querySelector('section')).not.toHaveAttribute('aria-labelledby')
    expect(screen.getByText('body').closest('[data-slot="panel-body"]')).not.toBeNull()
  })

  it('names the section by its title and keeps a right slot', () => {
    render(
      <Panel title="Recent runs" right={<button type="button">Refresh</button>}>
        rows
      </Panel>,
    )

    expect(screen.getByRole('region', { name: 'Recent runs' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
  })
})

describe('Field for forms with an error summary', () => {
  it('ids the message after its control, so a summary can link to it', () => {
    render(
      <Field label="Name" htmlFor="metric-name" error="Name is required">
        <TextInput id="metric-name" value="" onChange={() => {}} />
      </Field>,
    )

    expect(document.getElementById('metric-name-error')).toHaveTextContent('Name is required')
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveAttribute('aria-describedby', 'metric-name-error')
  })

  it('leaves announcing to the summary when told to', () => {
    render(
      <Field label="Name" htmlFor="n" error="Name is required" announceError={false}>
        <TextInput id="n" value="" onChange={() => {}} />
      </Field>,
    )

    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveAttribute('aria-invalid', 'true')
  })

  it('takes an explicit error id', () => {
    render(
      <Field label="Group" htmlFor={false} error="Pick one" errorId="group-error">
        <span>choices</span>
      </Field>,
    )

    expect(document.getElementById('group-error')).toHaveTextContent('Pick one')
  })
})

describe('Kit page headers share PageHeader', () => {
  it('renders SHeader and PageHead with the same h1', () => {
    const { unmount } = render(<SHeader title="Members" description="Who can sign in." />)
    const shHeading = screen.getByRole('heading', { level: 1, name: 'Members' })
    const shClass = shHeading.className
    unmount()

    render(<PageHead eyebrow="Govern" title="Coverage" right={<button type="button">Run</button>} />)
    const phHeading = screen.getByRole('heading', { level: 1, name: 'Coverage' })
    expect(phHeading.className).toBe(shClass)
    expect(screen.getByText('Govern')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run' })).toBeInTheDocument()
  })
})

// Panel, SCard and ui/Card share one section-card geometry.
describe('Section card geometry', () => {
  it('gives SCard the Panel radius, gutter and title size', () => {
    const { container } = render(
      <SCard title="Project details" footer={<button type="button">Save</button>}>
        <div>body</div>
      </SCard>,
    )
    expect(container.querySelector('section')).toHaveClass('rounded-card')
    expect(container.querySelector('header')).toHaveClass('px-4', 'py-3')
    expect(screen.getByRole('heading', { name: 'Project details' })).toHaveClass('text-body-sm', 'font-semibold')
    expect(container.querySelector('footer')).toHaveClass('px-4')
  })

  it('renders a Panel footer under the body only when given one', () => {
    const { container, rerender } = render(<Panel title="Destinations">rows</Panel>)
    expect(container.querySelector('[data-slot="panel-footer"]')).toBeNull()

    rerender(
      <Panel title="Destinations" footer={<button type="button">Test</button>}>
        rows
      </Panel>,
    )
    const footer = container.querySelector('[data-slot="panel-footer"]')
    expect(footer).toContainElement(screen.getByRole('button', { name: 'Test' }))
    expect(container.querySelector('[data-slot="panel-body"]')?.nextElementSibling).toBe(footer)
  })
})

// one save model for a settings page.
describe('SettingsSaveBar', () => {
  it('disables both actions until the draft is dirty', () => {
    render(<SettingsSaveBar note="Applies on save." dirty={false} onDiscard={() => {}} onSave={() => {}} />)
    expect(screen.getByText('Applies on save.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Discard' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })

  it('saves and discards a dirty draft', () => {
    const onSave = vi.fn()
    const onDiscard = vi.fn()
    render(<SettingsSaveBar dirty onDiscard={onDiscard} onSave={onSave} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(onSave).toHaveBeenCalledTimes(1)
    expect(onDiscard).toHaveBeenCalledTimes(1)
  })

  it('blocks Save on an invalid draft and says why', () => {
    render(<SettingsSaveBar dirty invalid onDiscard={() => {}} onSave={() => {}} />)
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Discard' })).toBeEnabled()
    expect(screen.getByText('Fix the highlighted fields to save.')).toBeInTheDocument()
  })

  it('announces a save error and shows progress while saving', () => {
    render(
      <SettingsSaveBar
        dirty
        pending
        error="Save failed"
        warning="Also unsaved: Email."
        onDiscard={() => {}}
        onSave={() => {}}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Save failed')
    expect(screen.getByText('Also unsaved: Email.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Saving...' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Discard' })).toBeDisabled()
  })
})

describe('NativeSelect width', () => {
  it('caps a compact select at 280px and lets a fill select take the control column', () => {
    const { rerender } = render(
      <NativeSelect aria-label="Mode" value="a" onChange={() => {}} options={['a', 'b']} />,
    )
    expect(screen.getByRole('combobox', { name: 'Mode' }).parentElement).toHaveStyle({ maxWidth: '280px' })

    rerender(
      <NativeSelect aria-label="Mode" value="a" onChange={() => {}} options={['a', 'b']} width="fill" />,
    )
    expect(screen.getByRole('combobox', { name: 'Mode' }).parentElement?.style.maxWidth).toBe('')
  })
})

describe('NativeSelect options, groups, size and ref', () => {
  it('renders disabled options and optgroups after the plain options', () => {
    render(
      <NativeSelect
        aria-label="Event"
        value=""
        options={[{ value: '', label: 'Choose one', disabled: true }]}
        groups={[
          { label: 'Events', options: [{ value: 'e1', label: 'Signup' }] },
          { label: 'Types', options: ['t1'] },
        ]}
      />,
    )
    const select = screen.getByRole('combobox', { name: 'Event' })
    expect(screen.getByRole('option', { name: 'Choose one' })).toBeDisabled()
    const groups = select.querySelectorAll('optgroup')
    expect([...groups].map((g) => g.label)).toEqual(['Events', 'Types'])
    expect(groups[0]).toContainElement(screen.getByRole('option', { name: 'Signup' }))
    expect(groups[1]).toContainElement(screen.getByRole('option', { name: 't1' }))
  })

  it('is 28px tall at size sm and forwards its ref', () => {
    const ref = { current: null as HTMLSelectElement | null }
    const { rerender } = render(
      <NativeSelect aria-label="Mode" value="a" onChange={() => {}} options={['a']} ref={ref} />,
    )
    expect(ref.current).toBe(screen.getByRole('combobox', { name: 'Mode' }))
    expect(ref.current).toHaveStyle({ height: '32px' })

    rerender(<NativeSelect aria-label="Mode" value="a" onChange={() => {}} options={['a']} ref={ref} size="sm" />)
    expect(ref.current).toHaveStyle({ height: '28px' })
  })
})

describe('TextArea autoGrow', () => {
  it('sizes to its content between the given rows and eight lines', () => {
    const { rerender } = render(<TextArea value="text" onChange={() => {}} rows={3} />)
    expect(screen.getByRole('textbox')).not.toHaveClass('field-sizing-content')

    rerender(<TextArea value="text" onChange={() => {}} rows={3} autoGrow />)
    expect(screen.getByRole('textbox')).toHaveClass('field-sizing-content')
  })
})
