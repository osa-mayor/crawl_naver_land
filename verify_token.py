import json
from upload_drive import authenticate_drive_oauth

def verify_token():
    try:
        with open('token_output.txt', 'r') as f:
            token_json = f.read()
        
        print("🔍 토큰 유효성 검사 중...")
        service = authenticate_drive_oauth(token_json)
        
        if service:
            # 간단한 API 호출로 실제 권한 확인
            about = service.about().get(fields="user, storageQuota").execute()
            user = about['user']
            quota = about['storageQuota']
            
            print("\n✅ 인증 성공! (토큰이 정상적으로 작동합니다)")
            print(f"👤 사용자: {user['displayName']} ({user['emailAddress']})")
            
            # 용량 확인 (바이트 -> GB 변환)
            limit = int(quota.get('limit', 0))
            usage = int(quota.get('usage', 0))
            if limit > 0:
                usage_gb = usage / (1024**3)
                limit_gb = limit / (1024**3)
                print(f"💾 사용 용량: {usage_gb:.2f} GB / {limit_gb:.2f} GB ({usage/limit*100:.1f}%)")
            
            print("\n이제 GitHub Actions에서도 정상적으로 작동할 것입니다.")
        else:
            print("❌ 서비스 객체 생성 실패")
            
    except Exception as e:
        print(f"\n❌ 검증 실패: {e}")
        print("토큰 파일(token_output.txt)이 손상되었거나 만료되었을 수 있습니다.")

if __name__ == "__main__":
    verify_token()
