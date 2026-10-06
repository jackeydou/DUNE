import { Link, useRouterState } from "@tanstack/react-router"
import {
  ChevronsUpDown,
  FlaskConical,
  Hexagon,
  KeyRound,
  Library,
  LogOut,
  ScanSearch,
  Users,
} from "lucide-react"
import type { ComponentType } from "react"

import { auth } from "@/api"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar"
import type { User } from "@/gen/swarmeval/api/v1/auth_pb"
import { isAdmin } from "@/session"

interface Item {
  to: "/runs" | "/cases" | "/analysis" | "/users"
  label: string
  icon: ComponentType
  /** Other paths whose pages belong under this item. */
  also?: string[]
}

const GROUPS: { label: string; items: Item[]; admin?: boolean }[] = [
  {
    label: "Evaluate",
    items: [
      { to: "/runs", label: "Runs", icon: FlaskConical, also: ["/compare"] },
      { to: "/cases", label: "Cases", icon: Library },
    ],
  },
  {
    label: "Investigate",
    items: [{ to: "/analysis", label: "Analysis", icon: ScanSearch }],
  },
  {
    label: "Admin",
    admin: true,
    items: [{ to: "/users", label: "Users", icon: Users }],
  },
]

async function signOut() {
  try {
    await auth.logout({})
  } finally {
    // A full load, also when the session had already ended: nothing the signed-out user read
    // stays in memory.
    window.location.assign("/")
  }
}

export function AppSidebar({ user }: { user: User }) {
  const path = useRouterState({ select: (s) => s.location.pathname })
  const active = (item: Item) =>
    [item.to, ...(item.also ?? [])].some(
      (p) => path === p || path.startsWith(`${p}/`)
    )
  return (
    <Sidebar collapsible="icon" data-testid="nav">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" asChild>
              <Link to="/runs">
                <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                  <Hexagon className="size-4" />
                </div>
                <div className="grid flex-1 text-left leading-tight">
                  <span className="truncate font-heading text-base font-semibold">
                    SwarmEval
                  </span>
                  <span className="truncate text-xs text-muted-foreground">
                    Evaluation console
                  </span>
                </div>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        {GROUPS.filter((g) => !g.admin || isAdmin(user)).map((group) => (
          <SidebarGroup key={group.label}>
            <SidebarGroupLabel>{group.label}</SidebarGroupLabel>
            <SidebarMenu>
              {group.items.map((item) => (
                <SidebarMenuItem key={item.to}>
                  <SidebarMenuButton
                    asChild
                    isActive={active(item)}
                    tooltip={item.label}
                  >
                    <Link to={item.to}>
                      <item.icon />
                      <span>{item.label}</span>
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroup>
        ))}
      </SidebarContent>
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <SidebarMenuButton
                  size="lg"
                  aria-label="Your account"
                  className="data-[state=open]:bg-sidebar-accent"
                >
                  <Avatar className="size-8 rounded-lg">
                    <AvatarFallback className="rounded-lg bg-primary/15 text-primary">
                      {user.username.slice(0, 2).toUpperCase()}
                    </AvatarFallback>
                  </Avatar>
                  <div className="grid flex-1 text-left text-sm leading-tight">
                    <span className="truncate font-medium" data-testid="whoami">
                      {user.username}
                    </span>
                    <span className="truncate text-xs text-muted-foreground">
                      {isAdmin(user) ? "Admin" : "Member"}
                    </span>
                  </div>
                  <ChevronsUpDown className="ml-auto size-4" />
                </SidebarMenuButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent className="w-56" side="top" align="start">
                <DropdownMenuLabel className="font-normal text-muted-foreground">
                  Signed in as {user.username}
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem asChild>
                  <Link to="/account">
                    <KeyRound /> Password and tokens
                  </Link>
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => void signOut()}>
                  <LogOut /> Sign out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
