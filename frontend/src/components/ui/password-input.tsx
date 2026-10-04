import { useState, type ComponentProps } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

/**
 * A password field with a show/hide toggle (#250). The 12-character
 * policy makes a typo likely on register, reset and invite, and a masked field
 * gave no way to check one. The toggle is a pressed/unpressed button with one
 * fixed name, so a screen reader hears "Show password, toggle button, pressed"
 * rather than a name that flips under it.
 */
export function PasswordInput({ className, ...props }: Omit<ComponentProps<typeof Input>, 'type'>) {
  const [visible, setVisible] = useState(false)
  return (
    <div className="relative">
      <Input {...props} type={visible ? 'text' : 'password'} className={cn('pr-9', className)} />
      <button
        type="button"
        aria-label="Show password"
        aria-pressed={visible}
        aria-controls={props.id}
        onClick={() => setVisible((current) => !current)}
        className="absolute inset-y-0 right-0 flex w-8 items-center justify-center rounded-r-control text-fg-subtle transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
      >
        {visible ? (
          <EyeOff aria-hidden="true" className="h-3.5 w-3.5" />
        ) : (
          <Eye aria-hidden="true" className="h-3.5 w-3.5" />
        )}
      </button>
    </div>
  )
}
