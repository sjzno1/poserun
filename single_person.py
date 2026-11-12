"""Compatibility names for the automatic single-primary-runner product gate.

Background people are allowed; automatic choice is conservative and unvalidated.
No manual selection, multiple-subject report, re-identification or reacquisition.
"""
SCOPE = 'single_primary_runner'
PRIMARY_SUBJECT_FAILURE_STATUSES = frozenset(('primary_subject_ambiguous', 'primary_subject_lost'))
PRIMARY_SUBJECT_UNKNOWN_STATUSES = frozenset(('no_pose', 'primary_subject_unknown'))


class SinglePersonVideoRequired(ValueError):
    """Historical import retained. Signature: (frame_index=None,
    error_code='primary_subject_ambiguous', reason=None)."""
    error_code = 'primary_subject_ambiguous'

    def __init__(self, frame_index=None, error_code='primary_subject_ambiguous', reason=None):
        if error_code not in PRIMARY_SUBJECT_FAILURE_STATUSES:
            raise ValueError('invalid primary-subject error code')
        self.frame_index = frame_index
        self.error_code = error_code
        self.reason = reason
        message = ('无法明确自动选择唯一主跑者，已停止整段视频分析。请使用主跑者明显突出、与其他人分离的视频。'
                   if error_code == 'primary_subject_ambiguous' else
                   '主跑者连续锁定已丢失或出现歧义，已停止整段视频分析；不会自动换人或重新识别。')
        super().__init__(message)

    def payload(self):
        result: dict[str, object] = {'error': str(self), 'error_code': self.error_code, 'analysis_scope': SCOPE}
        if self.reason is not None:
            result['reason'] = self.reason
        if self.frame_index is not None:
            result['frame_index'] = self.frame_index
            result['frame_index_base'] = 0
        return result


def require_single_person_request(data):
    if 'target_selection' in data or 'target_selections' in data:
        raise ValueError('本系统自动选择并连续锁定一名主跑者（非仅限单人视频），不支持手动框选；请移除target_selection/target_selections参数。')
