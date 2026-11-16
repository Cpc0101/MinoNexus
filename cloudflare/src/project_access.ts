/** Project data is shared; task and node identity remains the initiating user. */
export const ACCESS_SQL = `(p.owner_user_id=? OR EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id=p.project_id AND pm.user_id=?))`;
export class ProjectAccessError extends Error { constructor(readonly http: number, message: string) { super(message); } }
export async function projectOwner(db: D1Database, project: string, user: string): Promise<string | null> {
  const row = await db.prepare(`SELECT p.owner_user_id FROM projects p WHERE p.project_id=? AND ${ACCESS_SQL}`).bind(project,user,user).first<{owner_user_id:string}>();
  return row?.owner_user_id || null;
}
export async function requireProjectOwner(db: D1Database, project: string, user: string): Promise<string> {
  const owner = await projectOwner(db,project,user);
  if (!owner) throw new ProjectAccessError(404,'项目不存在或不可访问');
  return owner;
}
export async function listMembers(db: D1Database, project: string, user: string) {
  const owner = await requireProjectOwner(db,project,user);
  const rows = await db.prepare(`SELECT u.user_id,u.email,'owner' AS role,p.created_at_ms AS added_at FROM projects p JOIN users u ON u.user_id=p.owner_user_id WHERE p.project_id=? UNION ALL SELECT u.user_id,u.email,'member' AS role,m.added_at_ms AS added_at FROM project_members m JOIN users u ON u.user_id=m.user_id WHERE m.project_id=? ORDER BY role DESC,added_at`).bind(project,project).all();
  return {members:rows.results || [],can_manage:owner === user};
}
async function manage(db: D1Database, project: string, user: string) {
  if (await requireProjectOwner(db,project,user) !== user) throw new ProjectAccessError(403,'只有项目所有者可以管理成员');
}
export async function addMember(db: D1Database, project: string, user: string, email: string) {
  await manage(db,project,user);
  email = String(email || '').trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email.length > 254) throw new ProjectAccessError(422,'请输入有效邮箱');
  const target = await db.prepare('SELECT user_id FROM users WHERE lower(email)=?').bind(email).first<{user_id:string}>();
  if (!target) throw new ProjectAccessError(404,'该邮箱尚未注册');
  if (target.user_id === user) throw new ProjectAccessError(409,'该账号已是项目所有者');
  await db.prepare(`INSERT INTO project_members(project_id,user_id,added_by,added_at_ms) SELECT ?,?,?,? WHERE EXISTS(SELECT 1 FROM projects WHERE project_id=? AND owner_user_id=?) ON CONFLICT(project_id,user_id) DO NOTHING`).bind(project,target.user_id,user,Date.now(),project,user).run();
  return listMembers(db,project,user);
}
export async function removeMember(db: D1Database, project: string, user: string, member: string) {
  await manage(db,project,user);
  if (member === user) throw new ProjectAccessError(409,'不能移除项目所有者');
  await db.prepare('DELETE FROM project_members WHERE project_id=? AND user_id=?').bind(project,member).run();
  return listMembers(db,project,user);
}
export async function canReadRun(db: D1Database, user: string, run: {owner_user_id:string;project_id:string}) {
  return Boolean(await projectOwner(db,run.project_id,user));
}
