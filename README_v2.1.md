# SMT Manager v2.1 (개선판)

## 달라진 점
- 원본 v2.0의 WMI 이벤트 기반 즉시 감지를 유지
- 프로세스 이름 인식 강화: `heroes`, `heroes.exe`를 같은 이름으로 처리
- 설정 자동 보정: 잘못된 값이 들어와도 안전한 기본값으로 복구
- 로그 파일 자동 관리: `smt_log.txt`가 너무 커지면 자동으로 회전 보관
- pywin32가 없는 환경에서는 자동으로 폴링 모드로 폴백

## 실행 방법
1. 관리자 권한으로 `SMTManager_v2_1.py` 실행
2. 설정 창에서 게임 프로세스 이름/SMT 옵션/마스크 저장
3. 창을 닫으면 트레이로 숨김, 트레이 메뉴에서 다시 열기 가능

## 설정 파일
파일: `smt_config.json`

```json
{
  "enabled": true,
  "check_interval": 30,
  "game_processes": ["Client", "heroes", "heroes_x64"],
  "custom_mask": ""
}
```

## 참고
- 이 파일은 Python 소스 버전입니다.
- 기존 `SMTManager_v2.0.exe`는 그대로 유지됩니다.
