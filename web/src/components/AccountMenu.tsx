import { useEffect, useState } from 'react'
import { api, signInAgain } from '../api'

type AccountMenuProps = {
  onClose: () => void
}

export const AccountMenu = ({ onClose }: AccountMenuProps) => {
  const [name, setName] = useState<string | null>(null)
  const [signingOut, setSigningOut] = useState(false)

  useEffect(() => {
    void api.me().then((me) => setName(me.name))
  }, [])

  const signOut = async () => {
    setSigningOut(true)
    await api.logout()
    signInAgain()
  }

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="menu-sheet" role="menu" onClick={(event) => event.stopPropagation()}>
        <div className="sheet-grip" />
        <div className="account-info">
          <span className="account-name">{name ?? '…'}</span>
          <span className="account-hint">Your projects are private to you.</span>
        </div>
        <button className="menu-item danger" role="menuitem" disabled={signingOut} onClick={signOut}>
          Sign out
        </button>
      </div>
    </div>
  )
}
