""" 清言群管插件
提供智能禁言功能的群聊管理插件。
功能特性：
- 智能LLM判定：根据聊天内容智能判断是否需要禁言
- 灵活的时长管理：支持自定义禁言时长限制
- 参数验证：完整的输入参数验证和错误处理
- 配置文件支持：所有设置可通过配置文件调整
- 权限管理：支持用户权限和群组权限控制
- 自然回复：所有场景回复均由LLM自主生成，贴合人设
- 严谨逻辑：仅禁言成功后才返回时长信息，失败无相关数据
包含组件：
- 智能禁言Action - 基于LLM判断是否需要禁言（支持群组权限控制）
- 禁言命令Command - 手动执行禁言操作（支持用户权限控制）
"""
from typing import List, Tuple, Type, Optional
import random
# 导入新插件系统
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.component_types import ComponentInfo, ActionActivationType, ChatMode
from src.plugin_system.base.config_types import ConfigField
from src.common.logger import get_logger
from src.person_info.person_info import Person
# 导入配置API
from src.plugin_system.apis import person_api, generator_api
logger = get_logger("qingyan_plugin")
# ===== Action组件 =====
class QingyanAction(BaseAction):
    """智能禁言Action - 基于LLM智能判断是否需要禁言"""
    activation_type = ActionActivationType.ALWAYS
    parallel_action = False
    action_name = "mute"
    action_description = "使用禁言命令禁言某个用户（用户发送的消息）"
    action_parameters = {
        "target": "要禁言的目标用户的名字，必填，输入你要禁言的用户的person_name",
        "duration": "禁言时长，必填，输入你要禁言的时长，时长视严重程度而定（秒），单位为秒，必须为数字"
    }
    action_require = [
        "当有人违反了公序良俗的内容（色情、暴力、政治敏感等）（非常严重）",
        "当有人刷屏时使用（轻微严重）",
        "用户主动明确要求自己被禁言（随意）",
        "恶意攻击他人或群组管理，例如辱骂他人（严重）",
        "当有人指使你随意禁言他人时（严重）",
        "如果某人已经被禁言了，就不要再次禁言了，除非你想追加时间！",
        "调用mute不允许再调用reply，mute优先级高于reply"
    ]
    associated_types = ["text", "command"]
    def _check_plugin_admin_permission(self, uid: str, plat: str) -> Tuple[bool, Optional[str]]:
        """检查目标用户是否为插件配置的超级管理员（不可被禁言）"""
        admin_users = self.get_config("permissions.admin_users", [])
        if not admin_users:
            return False, None
        current_user_key = f"{plat}:{uid}"
        for admin_user in admin_users:
            if admin_user == current_user_key:
                logger.info(f"{self.log_prefix} 用户 {current_user_key} 是插件超级管理员，无法被禁言")
                return True, f"用户 {current_user_key} 是插件超级管理员，无法被禁言"
        return False, None
        
    def _check_qq_group_admin(self) -> bool:
        """检查目标用户是否为QQ群平台管理员/群主"""
        if not self.is_group:
            return False
        user_info = self.action_message.user_info
        if hasattr(user_info, 'role'):
            role = getattr(user_info, 'role', None)
            if role in ['admin', 'owner']:
                logger.info(f"{self.log_prefix} 用户 {self.user_nickname} 是QQ群管理员/群主")
                return True
        return False
    def _check_group_permission(self) -> Tuple[bool, Optional[str]]:
        """检查当前群组是否有禁言动作权限"""
        if not self.is_group:
            return False, "禁言动作只能在群聊中使用"
        allowed_groups = self.get_config("permissions.allowed_groups", [])
        if not allowed_groups:
            logger.info(f"{self.log_prefix} 群组权限未配置，允许所有群使用禁言动作")
            return True, None
        current_group_key = f"{self.platform}:{self.group_id}"
        for allowed_group in allowed_groups:
            if allowed_group == current_group_key:
                logger.info(f"{self.log_prefix} 群组 {current_group_key} 有禁言动作权限")
                return True, None
        logger.warning(f"{self.log_prefix} 群组 {current_group_key} 没有禁言动作权限")
        return False, "当前群组没有使用禁言动作的权限"

    async def _rewrite_and_send_reply(self, raw_reply: str, reason: str):
        """统一封装：LLM重写回复并发送，失败则降级发送原始回复"""
        try:
            result_status, data = await generator_api.rewrite_reply(
                chat_stream=self.chat_stream,
                reply_data={
                    "raw_reply": raw_reply,
                    "reason": reason,
                },
            )
            if result_status:
                for reply_seg in data.reply_set.reply_data:
                    await self.send_text(reply_seg.content)
            else:
                await self.send_text(raw_reply)
        except Exception as e:
            logger.error(f"[回复重写异常] {str(e)}")
            await self.send_text(raw_reply)
    async def execute(self) -> Tuple[bool, Optional[str]]:
        """禁言动作主执行逻辑"""
        logger.info(f"{self.log_prefix} 开始执行智能禁言动作")
        # ========== 群组权限校验 ==========
        has_group_perm, group_perm_msg = self._check_group_permission()
        if not has_group_perm:
            # 群组无权限回复交由LLM重写
            await self._rewrite_and_send_reply(
                raw_reply="我没有在这个群里执行禁言的权限哦",
                reason=f"告知用户我没有当前群组的禁言权限，无法执行操作，原因：{group_perm_msg}"
            )
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"尝试执行禁言操作，因当前群组无权限被拦截",
                action_done=False,
            )
            return False, group_perm_msg
        # ========== 第三层：参数校验 ==========
        duration = self.action_data.get("duration")
        reason = self.action_data.get("reason", "违反群规")
        target = self.action_data.get("target")
        # 时长参数校验
        if not duration:
            error_msg = "禁言时长参数不能为空"
            logger.error(f"{self.log_prefix} {error_msg}")
            await self._rewrite_and_send_reply(
                raw_reply="你没有告诉我要禁言多长时间哦",
                reason="告知用户禁言操作缺少时长参数，需要指定禁言的时长"
            )
            return False, error_msg
        # 时长格式与范围校验
        min_duration = self.get_config("qingyan.min_duration", 60)
        max_duration = self.get_config("qingyan.max_duration", 2592000)
        try:
            duration_int = int(duration)
            if duration_int <= 0:
                error_msg = "禁言时长必须为大于0的数字"
                await self._rewrite_and_send_reply(
                    raw_reply="禁言时长必须是大于0的正数哦",
                    reason="告知用户禁言时长必须为大于0的有效数字"
                )
                return False, error_msg
            # 时长范围自动修正
            if duration_int < min_duration:
                duration_int = min_duration
                logger.info(f"{self.log_prefix} 禁言时长过短，自动修正为{min_duration}秒")
            elif duration_int > max_duration:
                duration_int = max_duration
                logger.info(f"{self.log_prefix} 禁言时长过长，自动修正为{max_duration}秒")
        except (ValueError, TypeError):
            error_msg = f"禁言时长格式无效: {duration}"
            logger.error(f"{self.log_prefix} {error_msg}")
            await self._rewrite_and_send_reply(
                raw_reply="禁言时长必须是有效的数字哦",
                reason="告知用户输入的禁言时长格式错误，必须为数字"
            )
            return False, error_msg
        # 目标用户校验与处理
        if not target:
            error_msg = "禁言目标用户不能为空"
            logger.error(f"{self.log_prefix} {error_msg}")
            await self._rewrite_and_send_reply(
                raw_reply="你没有告诉我要禁言谁哦",
                reason="告知用户禁言操作缺少目标用户，需要指定要禁言的对象"
            )
            return False, error_msg
        # 处理@开头的用户名
        if target.startswith('@'):
            target = target[1:]

        # 处理首尾的尖括号（如<用户名:QQ号>格式）
        target = target.strip('<>')

        # 处理全角冒号，统一转换为半角冒号，兼容不同的输入格式
        target = target.replace('：', ':')

        # 处理 "昵称:QQ号" 这种格式的目标用户，自动提取user_id
        target_uid = None
        # 检查是否包含冒号，且冒号后为纯数字（QQ号格式）
        if ':' in target:
            parts = target.split(':', 1)  # 只分割一次，避免昵称里有冒号的情况
            if len(parts) == 2 and parts[1].strip().isdigit():
                # 提取后面的数字作为user_id
                target_uid = parts[1].strip()
                logger.info(f"[人物] 从目标用户格式中提取到user_id: {target_uid}，原始target: {target}")

        # 提取显示用的昵称，去掉后面的QQ号部分，用于回复展示
        target_name = target.split(':', 1)[0].strip()

        # 如果没有提取到user_id，再通过名字查找
        if target_uid is None:
            person_id = person_api.get_person_id_by_name(target)
            target_uid = await person_api.get_person_value(person_id, "user_id")

        target_person = Person(platform=self.platform, user_id=target_uid)
        target_person_name = target_person.person_name
        # 目标用户不存在校验
        if not target_uid or target_uid == "unknown":
            error_msg = f"未找到目标用户 {target} 的有效信息"
            logger.error(f"{self.log_prefix} {error_msg}")
            await self._rewrite_and_send_reply(
                raw_reply=f"我找不到用户 {target} 哦，无法执行禁言操作",
                reason="告知用户找不到指定的禁言目标，无法执行禁言操作"
            )
            return False, error_msg
        # ========== 第四层：目标用户豁免校验 ==========
        # 校验目标是否为插件超级管理员
        is_super_admin, super_admin_msg = self._check_plugin_admin_permission(str(target_uid), self.platform)
        if is_super_admin:
            await self._rewrite_and_send_reply(
                raw_reply="这个用户是超级管理员，我不能禁言他哦",
                reason="告知用户目标用户是插件超级管理员，拥有禁言豁免权限，无法执行禁言"
            )
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"尝试禁言用户 {target_person_name}，因该用户是超级管理员被拦截",
                action_done=False,
            )
            return False, super_admin_msg
        # 校验目标是否为QQ群管理员/群主，50%概率拦截
        is_group_admin = self._check_qq_group_admin()
        if is_group_admin:
            if random.random() < 0.5:
                await self._rewrite_and_send_reply(
                    raw_reply="这个用户是群管理员，我不能禁言他哦",
                    reason="告知用户目标用户是群管理员，拥有禁言豁免权限，无法执行禁言"
                )
                await self.store_action_info(
                    action_build_into_prompt=True,
                    action_prompt_display=f"尝试禁言用户 {target_person_name}，因该用户是群管理员被拦截",
                    action_done=False,
                )
                return False, "目标用户为群管理员，无法执行禁言"
            else:
                reason = f"强制封禁群管理员，原因为：{reason}"
                logger.info(f"{self.log_prefix} 触发强制封禁，目标为群管理员 {target_person_name}")
        # ========== 第五层：执行禁言命令 ==========
        logger.info(f"{self.log_prefix} 开始发送禁言命令，目标用户 {target_person_name}({target_uid})，时长 {duration_int} 秒")
        ban_success = await self.send_command(
            command_name="GROUP_BAN",
            args={"qq_id": str(target_uid), "duration": str(duration_int)},
            storage_message=False
        )
        # ========== 第六层：执行结果处理 ==========
        # 禁言成功场景
        if ban_success:
            logger.info(f"{self.log_prefix} 禁言命令执行成功，用户 {target_person_name}({target_uid})，时长 {duration_int} 秒")
            # 格式化时长用于回复
            time_str = self._format_duration(duration_int)
            # 移除固定模板，由模型自主生成回复
            await self._rewrite_and_send_reply(
                raw_reply="",
                reason=f"告知用户，已成功对用户{target_name}执行禁言操作，禁言时长为{time_str}，本次禁言的原因是：{reason}。你可以自由决定回复的内容与语气，无需使用固定模板，完全由你自主生成符合当前场景的回复。"
            )
            # 记录成功动作
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"成功禁言用户 {target_name}，时长 {time_str}，原因：{reason}",
                action_done=True,
            )
            return True, f"成功禁言 {target_person_name}，时长 {time_str}"
        # 禁言失败场景
        else:
            error_msg = "禁言命令发送至平台失败，无法完成禁言操作"
            logger.error(f"{self.log_prefix} {error_msg}")
            await self._rewrite_and_send_reply(
                raw_reply="很抱歉，本次禁言操作执行失败了",
                reason="告知用户本次禁言操作执行失败，无法完成禁言"
            )
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"尝试禁言用户 {target_person_name} 失败，平台命令发送失败",
                action_done=False,
            )
            return False, error_msg
    def _format_duration(self, seconds: int) -> str:
        """将秒数格式化为可读的时间字符串（仅成功场景调用）"""
        if seconds < 60:
            return f"{seconds}秒"
        elif seconds < 3600:
            return f"{seconds//60}分钟"
        elif seconds < 86400:
            return f"{seconds//3600}小时"
        else:
            return f"{seconds//86400}天"
# ===== Command组件（手动禁言命令） =====
class QingyanCommand(BaseCommand):
    """手动禁言命令 - 仅授权管理员可使用"""
    command_name = "qy_command"
    command_description = "手动禁言命令，仅授权管理员可使用"
    command_pattern = r"^/qy\s+(?P<target>\S+)\s+(?P<duration>\d+)(?:\s+(?P<reason>.+))?$"
    command_help = "禁言指定用户，用法：/qy <用户名> <时长(秒)> [理由]"
    command_examples = ["/qy 用户名 300", "/qy 张三 600 刷屏", "/qy @某人 1800 违规内容"]
    intercept_message = True
    def _check_plugin_admin_permission(self, uid: str, plat: str) -> Tuple[bool, Optional[str]]:
        """检查目标用户是否为插件超级管理员"""
        admin_users = self.get_config("permissions.admin_users", [])
        if not admin_users:
            return False, None
        current_user_key = f"{plat}:{uid}"
        for admin_user in admin_users:
            if admin_user == current_user_key:
                return True, f"用户 {current_user_key} 是超级管理员，无法被禁言"
        return False, None
    def _check_user_permission(self) -> Tuple[bool, Optional[str]]:
        """检查命令执行者是否为授权管理员"""
        chat_stream = self.message.chat_stream
        if not chat_stream:
            return False, "无法获取聊天流信息"
        current_plat = chat_stream.platform
        current_uid = str(chat_stream.user_info.user_id)
        allowed_users = self.get_config("permissions.allowed_users", [])
        current_user_key = f"{current_plat}:{current_uid}"
        
        if not allowed_users:
            return False, "未配置授权管理员，禁止使用禁言命令"
            
        if current_user_key in allowed_users:
            return True, None
        return False, "你没有使用禁言命令的权限"
    async def _rewrite_and_send_reply(self, raw_reply: str, reason: str):
        """统一封装：LLM重写回复并发送"""
        try:
            result_status, data = await generator_api.rewrite_reply(
                chat_stream=self.message.chat_stream,
                reply_data={
                    "raw_reply": raw_reply,
                    "reason": reason,
                },
            )
            if result_status:
                for reply_seg in data.reply_set.reply_data:
                    await self.send_text(reply_seg.content)
            else:
                await self.send_text(raw_reply)
        except Exception as e:
            logger.error(f"[命令回复重写异常] {str(e)}")
            await self.send_text(raw_reply)
    async def execute(self) -> Tuple[bool, Optional[str], str]:
        """手动禁言命令主执行逻辑"""
        try:
            # 操作者权限校验
            has_permission, permission_error = self._check_user_permission()
            if not has_permission:
                await self._rewrite_and_send_reply(
                    raw_reply="你没有权限使用这个禁言命令哦",
                    reason=f"告知用户没有使用禁言命令的权限，原因：{permission_error}"
                )
                return False, permission_error, ""
            # 参数提取与处理
            target = self.matched_groups.get("target")
            duration = self.matched_groups.get("duration")
            reason = self.matched_groups.get("reason", "管理员操作")
            if target.startswith('@'):
                target = target[1:]

            # 处理首尾的尖括号（如<用户名:QQ号>格式）
            target = target.strip('<>')

            # 处理全角冒号，统一转换为半角冒号，兼容不同的输入格式
            target = target.replace('：', ':')

            # 处理 "昵称:QQ号" 这种格式的目标用户，自动提取user_id
            target_uid = None
            # 检查是否包含冒号，且冒号后为纯数字（QQ号格式）
            if ':' in target:
                parts = target.split(':', 1)  # 只分割一次，避免昵称里有冒号的情况
                if len(parts) == 2 and parts[1].strip().isdigit():
                    # 提取后面的数字作为user_id
                    target_uid = parts[1].strip()
                    logger.info(f"[人物] 从命令目标用户格式中提取到user_id: {target_uid}，原始target: {target}")

            # 提取显示用的昵称，去掉后面的QQ号部分，用于回复展示
            target_name = target.split(':', 1)[0].strip()

            # 参数完整性校验
            if not all([target, duration]):
                await self._rewrite_and_send_reply(
                    raw_reply="命令参数不完整，请检查格式，正确用法：/qy <用户名> <时长(秒)> [理由]",
                    reason="告知用户禁言命令的参数不完整，需要补充正确的参数"
                )
                return False, "参数不完整", ""
            # 时长校验
            min_duration = self.get_config("qingyan.min_duration", 60)
            max_duration = self.get_config("qingyan.max_duration", 2592000)
            try:
                duration_int = int(duration)
                if duration_int <= 0:
                    await self._rewrite_and_send_reply(
                        raw_reply="禁言时长必须是大于0的正数哦",
                        reason="告知用户禁言时长必须为大于0的有效数字"
                    )
                    return False, "时长无效", ""
                if duration_int < min_duration:
                    duration_int = min_duration
                elif duration_int > max_duration:
                    duration_int = max_duration
            except ValueError:
                await self._rewrite_and_send_reply(
                    raw_reply="禁言时长必须是有效的数字哦",
                    reason="告知用户输入的禁言时长格式错误，必须为数字"
                )
                return False, "时长格式错误", ""
            # 目标用户信息获取
            if target_uid is None:
                person_id = person_api.get_person_id_by_name(target)
                target_uid = await person_api.get_person_value(person_id, "user_id")
            if not target_uid or target_uid == "unknown":
                await self._rewrite_and_send_reply(
                    raw_reply=f"我找不到用户 {target} 哦，无法执行禁言",
                    reason="告知用户找不到指定的禁言目标，无法执行操作"
                )
                return False, "未找到目标用户", ""
            # 目标用户豁免校验
            is_super_admin, super_admin_msg = self._check_plugin_admin_permission(target_uid, self.message.chat_stream.platform)
            if is_super_admin:
                await self._rewrite_and_send_reply(
                    raw_reply="这个用户是超级管理员，无法被禁言哦",
                    reason="告知用户目标用户是超级管理员，拥有禁言豁免权限"
                )
                return False, super_admin_msg, ""
            # 格式化时长
            def format_duration(seconds: int) -> str:
                if seconds < 60:
                    return f"{seconds}秒"
                elif seconds < 3600:
                    return f"{seconds//60}分钟"
                elif seconds < 86400:
                    return f"{seconds//3600}小时"
                else:
                    return f"{seconds//86400}天"
            time_str = format_duration(duration_int)
            # 执行禁言命令
            ban_success = await self.send_command(
                command_name="GROUP_BAN",
                args={"qq_id": str(target_uid), "duration": str(duration_int)},
                display_message=f"禁言了 {target_name} {time_str}"
            )
            # 结果处理
            if ban_success:
                await self._rewrite_and_send_reply(
                    raw_reply="",
                    reason=f"告知用户，已成功对用户{target_name}执行禁言操作，禁言时长为{time_str}，本次禁言的原因是：{reason}。你可以自由决定回复的内容与语气，无需使用固定模板，完全由你自主生成符合当前场景的回复。"
                )
                return True, f"成功禁言 {target}，时长 {time_str}", ""
            else:
                await self._rewrite_and_send_reply(
                    raw_reply="很抱歉，禁言命令执行失败了",
                    reason="告知用户禁言命令发送至平台失败，无法完成禁言操作"
                )
                return False, "禁言命令执行失败", ""
        except Exception as e:
            logger.error(f"禁言命令执行异常: {str(e)}")
            await self._rewrite_and_send_reply(
                raw_reply="禁言命令执行过程中发生了异常",
                reason="告知用户禁言命令执行时出现异常，无法完成操作"
            )
            return False, str(e), ""
# ===== 插件主类 =====
@register_plugin
class QingyanPlugin(BasePlugin):
    plugin_name = "qingyan_plugin"
    enable_plugin = True
    config_file_name = "config.toml"
    dependencies = []
    python_dependencies = []
    config_section_descriptions = {
        "plugin": "插件基本信息配置",
        "components": "组件启用控制",
        "permissions": "权限管理配置",
        "qingyan": "核心功能配置",
        "logging": "日志记录相关配置",
    }
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="1.0.0", description="配置文件版本"),
        },
        "components": {
            "enable_qingyan_action": ConfigField(type=bool, default=True, description="是否启用智能禁言Action"),
            "enable_qingyan_command": ConfigField(type=bool, default=False, description="是否启用手动禁言命令Command"),
        },
        "permissions": {
            "admin_users": ConfigField(
                type=list,
                default=[],
                description="无法被禁言的超级管理员列表，格式：['plat:uid']，如['qq:123456789']",
            ),
            "allowed_users": ConfigField(
                type=list,
                default=[],
                description="有权限执行禁言操作的管理员列表，格式：['plat:uid']，如['qq:123456789']",
            ),
            "allowed_groups": ConfigField(
                type=list,
                default=[],
                description="允许使用禁言功能的群组列表，格式：['plat:gid']，如['qq:987654321']",
            ),
        },
        "qingyan": {
            "min_duration": ConfigField(type=int, default=60, description="最短禁言时长（秒）"),
            "max_duration": ConfigField(type=int, default=2592000, description="最长禁言时长（秒），默认30天"),
        },
    }
    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表"""
        enable_qingyan_action = self.get_config("components.enable_qingyan_action", True)
        enable_qingyan_command = self.get_config("components.enable_qingyan_command", False)
        components = []
        if enable_qingyan_action:
            components.append((QingyanAction.get_action_info(), QingyanAction))
        if enable_qingyan_command:
            components.append((QingyanCommand.get_command_info(), QingyanCommand))
        return components
