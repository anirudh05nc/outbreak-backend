import os
import csv
import time
from typing import List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import boto3
from botocore.exceptions import ClientError

app = FastAPI()

# Enable CORS for frontend integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Point boto3 to your local folder
current_dir = os.path.dirname(os.path.abspath(__file__))
os.environ['AWS_SHARED_CREDENTIALS_FILE'] = os.path.join(current_dir, 'credentials.ini')
os.environ['AWS_PROFILE'] = 'default'

# Now initialize your session
session = boto3.Session()

dynamodb = boto3.resource('dynamodb', region_name='ap-south-2') # Change to your region

TABLE_NAME = "outbreak26_teams"
table = dynamodb.Table(TABLE_NAME)

from botocore.config import Config
s3_client = boto3.client(
    's3',
    region_name='ap-south-2',
    endpoint_url='https://s3.ap-south-2.amazonaws.com',
    config=Config(signature_version='s3v4')
)
S3_BUCKET = os.environ.get("S3_BUCKET", "outbreak26-certificates")

class CertificateUploadRequest(BaseModel):
    team_id: str
    member_name: str
    file_name: str
    cert_type: str


class ProblemSelection(BaseModel):
    problem_title: str

class ToggleSelectionRequest(BaseModel):
    enabled: bool

class TimerLaunchRequest(BaseModel):
    duration: int

class DeployedLinkRequest(BaseModel):
    deployed_link: str

class TeamReviewRequest(BaseModel):
    status: str
    feedback: str
    score: int = 0

class ToggleDeleteProtectionRequest(BaseModel):
    enabled: bool

class TeamImportItem(BaseModel):
    TeamID: str
    TeamName: str
    Password: str
    LeaderName: str
    LeaderEmail: str
    LeaderPhone: str
    LeaderRegNo: str
    TransactionID: str = ""
    Status: str = "SUCCESS"
    SubmittedAt: str = ""

class ParticipantImportItem(BaseModel):
    TeamId: str
    Name: str
    RegNo: str
    Email: str
    Phone: str
    Gender: str
    Branch: str
    Year: int
    Accommodation: str = ""
    HostelName: str = ""
    RoomNo: str = ""
    WardenName: str = ""
    WardenPhone: str = ""

class ImportRequest(BaseModel):
    teams: List[TeamImportItem]
    participants: List[ParticipantImportItem]

class DeleteAllRequest(BaseModel):
    password: str

@app.get("/")
def read_root():
    return {"message": "Backend is running!"}


@app.get("/teams/all")
def get_all_items():
    try:
        response = table.scan()
        data = response.get('Items', [])
        filtered_data = [t for t in data if t.get('TeamID') != "SYSTEM_SETTINGS"]
        return {"count": len(filtered_data), "items": filtered_data}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.get("/teams/{partition_id}")
def get_single_item(partition_id: str):
    if partition_id == "SYSTEM_SETTINGS":
        raise HTTPException(status_code=404, detail="Team not found")
    try:
        response = table.get_item(
            Key={
                'TeamID': partition_id
            }
        )
        
        item = response.get('Item')
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
            
        return item
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/teams/{team_id}/select-problem")
def select_problem(team_id: str, selection: ProblemSelection):
    if team_id == "SYSTEM_SETTINGS":
        raise HTTPException(status_code=400, detail="Invalid team selection request")
    try:
        response = table.get_item(Key={'TeamID': team_id})
        team = response.get('Item')
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        
        current_selection = team.get('SelectedProblem')
        if current_selection:
            if current_selection == selection.problem_title:
                return {"message": "Problem already selected and locked.", "selected_problem": current_selection}
            raise HTTPException(status_code=400, detail="Challenge selection is locked and cannot be changed.")
        
        scan_resp = table.scan()
        all_teams = scan_resp.get('Items', [])
        count = sum(1 for t in all_teams if t.get('TeamID') != "SYSTEM_SETTINGS" and t.get('SelectedProblem') == selection.problem_title)
        
        if count >= 3:
            raise HTTPException(
                status_code=400, 
                detail=f"Challenge '{selection.problem_title}' is full. Maximum 3 teams allowed."
            )
        
        table.update_item(
            Key={'TeamID': team_id},
            UpdateExpression="set SelectedProblem = :val",
            ExpressionAttributeValues={':val': selection.problem_title}
        )
        
        return {"message": "Problem selection locked successfully.", "selected_problem": selection.problem_title}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.get("/problems/selection-counts")
def get_problem_selection_counts():
    try:
        response = table.scan()
        items = response.get('Items', [])
        counts = {}
        for item in items:
            if item.get('TeamID') == "SYSTEM_SETTINGS":
                continue
            prob = item.get('SelectedProblem')
            if prob:
                counts[prob] = counts.get(prob, 0) + 1
        return counts
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/problems/revoke-all")
def revoke_all_selections():
    try:
        settings_res = table.get_item(Key={'TeamID': 'SYSTEM_SETTINGS'})
        settings_item = settings_res.get('Item', {})
        if settings_item.get('DeleteProtectionActive', False):
            raise HTTPException(
                status_code=400, 
                detail="Action Denied: Delete Protection is currently active and preventing selection modifications."
            )
            
        response = table.scan()
        items = response.get('Items', [])
        
        for item in items:
            team_id = item.get('TeamID')
            if team_id == "SYSTEM_SETTINGS":
                continue
            if 'SelectedProblem' in item:
                table.update_item(
                    Key={'TeamID': team_id},
                    UpdateExpression="remove SelectedProblem"
                )
        return {"message": "All problem statements revoked successfully."}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.get("/settings")
def get_settings():
    try:
        response = table.get_item(Key={'TeamID': 'SYSTEM_SETTINGS'})
        item = response.get('Item')
        if not item:
            initial_settings = {
                'TeamID': 'SYSTEM_SETTINGS',
                'SelectionEnabled': False,
                'TimerLaunched': False,
                'TimerStartTime': 0,
                'TimerDuration': 0,
                'DeleteProtectionActive': False,
                'ProblemsCsvUploaded': False
            }
            table.put_item(Item=initial_settings)
            return {
                'SelectionEnabled': False,
                'TimerLaunched': False,
                'TimerStartTime': 0,
                'TimerDuration': 0,
                'DeleteProtectionActive': False,
                'ProblemsCsvUploaded': False
            }
        return {
            'SelectionEnabled': item.get('SelectionEnabled', False),
            'TimerLaunched': item.get('TimerLaunched', False),
            'TimerStartTime': int(item.get('TimerStartTime', 0)),
            'TimerDuration': int(item.get('TimerDuration', 0)),
            'DeleteProtectionActive': item.get('DeleteProtectionActive', False),
            'ProblemsCsvUploaded': item.get('ProblemsCsvUploaded', False)
        }
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/settings/toggle-selection")
def toggle_selection(req: ToggleSelectionRequest):
    try:
        # Gate: selection can only be enabled if problems CSV has been uploaded
        if req.enabled:
            settings_res = table.get_item(Key={'TeamID': 'SYSTEM_SETTINGS'})
            settings_item = settings_res.get('Item', {})
            if not settings_item.get('ProblemsCsvUploaded', False):
                raise HTTPException(
                    status_code=400,
                    detail="Action Denied: Problem Statements CSV has not been uploaded to S3 yet. Upload it first before enabling selection."
                )
        table.update_item(
            Key={'TeamID': 'SYSTEM_SETTINGS'},
            UpdateExpression="set SelectionEnabled = :val",
            ExpressionAttributeValues={':val': req.enabled}
        )
        return {"message": "Selection configuration updated successfully.", "enabled": req.enabled}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/settings/launch-timer")
def launch_timer(req: TimerLaunchRequest):
    import time
    expiry_time = int(time.time()) + req.duration
    try:
        # Gate: timer can only be launched if selection is enabled
        settings_res = table.get_item(Key={'TeamID': 'SYSTEM_SETTINGS'})
        settings_item = settings_res.get('Item', {})
        if not settings_item.get('SelectionEnabled', False):
            raise HTTPException(
                status_code=400,
                detail="Action Denied: Selection Gate must be enabled before launching the timer. Enable the Selection Gate first."
            )
        table.update_item(
            Key={'TeamID': 'SYSTEM_SETTINGS'},
            UpdateExpression="set TimerLaunched = :l, TimerStartTime = :s, TimerDuration = :d",
            ExpressionAttributeValues={
                ':l': True,
                ':s': expiry_time,
                ':d': req.duration
            }
        )
        return {"message": "Timer launched successfully.", "TimerStartTime": expiry_time, "TimerDuration": req.duration}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/settings/reset-timer")
def reset_timer():
    try:
        table.update_item(
            Key={'TeamID': 'SYSTEM_SETTINGS'},
            UpdateExpression="set TimerLaunched = :l, TimerStartTime = :s, TimerDuration = :d",
            ExpressionAttributeValues={
                ':l': False,
                ':s': 0,
                ':d': 0
            }
        )
        return {"message": "Timer reset successfully."}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/api/certificates/generate-upload-url")
def generate_upload_url(req: CertificateUploadRequest):
    try:
        # Validate certificate type
        if req.cert_type not in ["mongoDB", "Cloud"]:
            raise HTTPException(status_code=400, detail="Invalid certificate type. Must be 'mongoDB' or 'Cloud'.")

        # Get team item to extract registration ID and current certificates count
        res = table.get_item(Key={'TeamID': req.team_id})
        team = res.get('Item')
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        
        # Find member registration number from embedded list of members
        members = team.get('Members', [])
        reg_no = None
        for m in members:
            if m.get('name', '').lower().strip() == req.member_name.lower().strip():
                reg_no = m.get('regNo') or m.get('reg_no')
                break
        
        # Fallback to Leader RegNo if name matches Leader Name
        if not reg_no:
            if team.get('Leader Name', '').lower().strip() == req.member_name.lower().strip():
                reg_no = team.get('Leader RegNo')
                
        # Final fallback if registration number not found
        if not reg_no:
            reg_no = req.member_name.replace(" ", "_")
            
        # Count existing certificates for this member
        certs_map = team.get('Certificates', {})
        if not certs_map:
            certs_map = {}
        existing_certs = certs_map.get(req.member_name, [])
        if not isinstance(existing_certs, list):
            existing_certs = []
            
        # Construct the key according to the schema: certificates/team_{TeamId}/{RegNo}_{CertType}.pdf
        s3_key = f"certificates/team_{req.team_id}/{reg_no}_{req.cert_type}.pdf"
        
        # Limit checking and updating DB references
        if s3_key not in existing_certs:
            if len(existing_certs) >= 2:
                raise HTTPException(status_code=400, detail="Upload limit reached. Maximum 2 certificates allowed per participant.")
            
            # 1. Initialize Certificates Map if it does not exist
            table.update_item(
                Key={'TeamID': req.team_id},
                UpdateExpression="SET Certificates = if_not_exists(Certificates, :empty_map)",
                ExpressionAttributeValues={":empty_map": {}}
            )
            
            # 2. Append the new S3 key path string to Certificates[member_name]
            table.update_item(
                Key={'TeamID': req.team_id},
                UpdateExpression="SET Certificates.#member = list_append(if_not_exists(Certificates.#member, :empty_list), :new_key)",
                ExpressionAttributeNames={"#member": req.member_name},
                ExpressionAttributeValues={
                    ":new_key": [s3_key],
                    ":empty_list": []
                }
            )
        
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod='put_object',
            Params={
                'Bucket': S3_BUCKET,
                'Key': s3_key,
                'ContentType': 'application/pdf'
            },
            ExpiresIn=3600
        )
        
        return {
            "presigned_url": presigned_url,
            "s3_key": s3_key
        }
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


class CertificateDeleteRequest(BaseModel):
    team_id: str
    member_name: str
    s3_key: str
    password: str

@app.post("/api/admin/certificates/delete")
def delete_certificate(req: CertificateDeleteRequest):
    if req.password != "delete":
        raise HTTPException(status_code=403, detail="Unauthorized: Incorrect deletion authorization key.")
        
    try:
        # 1. Delete object from S3
        s3_client.delete_object(
            Bucket=S3_BUCKET,
            Key=req.s3_key
        )
        
        # 2. Get the team record to update the database mapping
        res = table.get_item(Key={'TeamID': req.team_id})
        team = res.get('Item')
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
            
        certs_map = team.get('Certificates', {})
        member_certs = certs_map.get(req.member_name, [])
        if req.s3_key in member_certs:
            member_certs.remove(req.s3_key)
            certs_map[req.member_name] = member_certs
            
            # Update the team record
            table.update_item(
                Key={'TeamID': req.team_id},
                UpdateExpression="SET Certificates = :certs",
                ExpressionAttributeValues={":certs": certs_map}
            )
            
        return {"message": "Certificate successfully deleted from S3 and database."}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.get("/api/admin/certificates/presign-get")
def presign_get_certificate(s3_key: str):
    try:
        url = s3_client.generate_presigned_url(
            ClientMethod='get_object',
            Params={
                'Bucket': S3_BUCKET,
                'Key': s3_key
            },
            ExpiresIn=3600
        )
        return {"url": url}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


class PurgeAllCertsRequest(BaseModel):
    password: str

@app.post("/api/admin/certificates/delete-all")
def purge_all_certificates(req: PurgeAllCertsRequest):
    if req.password != "delete":
        raise HTTPException(status_code=403, detail="Unauthorized: Incorrect deletion authorization key.")
    
    try:
        # 1. Scan DynamoDB to find all team records
        response = table.scan()
        items = response.get('Items', [])
        
        all_s3_keys = []
        for item in items:
            certs_map = item.get('Certificates', {})
            if isinstance(certs_map, dict):
                for member, keys in certs_map.items():
                    if isinstance(keys, list):
                        all_s3_keys.extend(keys)
        
        # 2. Bulk delete keys from S3 (boto3 delete_objects)
        if all_s3_keys:
            for i in range(0, len(all_s3_keys), 1000):
                chunk = all_s3_keys[i:i+1000]
                delete_objects = {'Objects': [{'Key': k} for k in chunk]}
                s3_client.delete_objects(
                    Bucket=S3_BUCKET,
                    Delete=delete_objects
                )
        
        # 3. Update DynamoDB items to remove Certificates mapping
        for item in items:
            team_id = item.get('TeamID')
            if team_id == 'SYSTEM_SETTINGS':
                continue
            table.update_item(
                Key={'TeamID': team_id},
                UpdateExpression="REMOVE Certificates"
            )
            
        return {"message": f"Successfully purged {len(all_s3_keys)} certificate files from S3 and database."}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])



# S3 key for the problem statements CSV
PROBLEMS_CSV_S3_KEY = "problemstatements/problems.csv"


@app.post("/api/problems/upload-csv")
def get_problems_csv_upload_url():
    """
    Generates a presigned S3 PUT URL for the admin to upload the problem
    statements CSV to S3 at problemstatements/problems.csv.
    After the URL is generated, marks ProblemsCsvUploaded = True in DynamoDB.
    """
    try:
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod='put_object',
            Params={
                'Bucket': S3_BUCKET,
                'Key': PROBLEMS_CSV_S3_KEY,
                'ContentType': 'text/csv'
            },
            ExpiresIn=3600
        )
        # Mark as uploaded in system settings (optimistic — actual S3 write happens client-side)
        table.update_item(
            Key={'TeamID': 'SYSTEM_SETTINGS'},
            UpdateExpression="set ProblemsCsvUploaded = :val",
            ExpressionAttributeValues={':val': True}
        )
        return {
            "presigned_url": presigned_url,
            "s3_key": PROBLEMS_CSV_S3_KEY,
            "message": "Presigned upload URL generated. Upload the CSV via PUT request to this URL."
        }
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.get("/api/problems/csv")
def get_problems_csv_download_url():
    """
    Generates a presigned S3 GET URL so the frontend can fetch the
    problem statements CSV directly from S3.
    """
    try:
        # Check if CSV has been uploaded
        settings_res = table.get_item(Key={'TeamID': 'SYSTEM_SETTINGS'})
        settings_item = settings_res.get('Item', {})
        if not settings_item.get('ProblemsCsvUploaded', False):
            raise HTTPException(
                status_code=404,
                detail="Problem statements CSV has not been uploaded yet."
            )
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod='get_object',
            Params={
                'Bucket': S3_BUCKET,
                'Key': PROBLEMS_CSV_S3_KEY
            },
            ExpiresIn=3600
        )
        return {"presigned_url": presigned_url}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/api/problems/reset-csv")
def reset_problems_csv():
    """
    Admin-only: Marks ProblemsCsvUploaded = False so the admin can re-upload.
    Also disables SelectionEnabled to maintain gate order.
    """
    try:
        table.update_item(
            Key={'TeamID': 'SYSTEM_SETTINGS'},
            UpdateExpression="set ProblemsCsvUploaded = :val, SelectionEnabled = :sel",
            ExpressionAttributeValues={':val': False, ':sel': False}
        )
        return {"message": "Problems CSV status reset. Admin must re-upload to enable selection."}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/teams/{team_id}/submit-link")
def submit_deployed_link(team_id: str, req: DeployedLinkRequest):
    if team_id == "SYSTEM_SETTINGS":
        raise HTTPException(status_code=400, detail="Invalid team request")
    try:
        link = req.deployed_link.strip()
        if not link.startswith("http://") and not link.startswith("https://"):
            raise HTTPException(status_code=400, detail="Invalid URL format. Must start with http:// or https://")
        
        table.update_item(
            Key={'TeamID': team_id},
            UpdateExpression="set DeployedLink = :val, LinkSubmittedAt = :ts",
            ExpressionAttributeValues={
                ':val': link,
                ':ts': int(time.time())
            }
        )
        return {"message": "Deployed link submitted successfully.", "deployed_link": link}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/teams/{team_id}/submit-review")
def submit_team_review(team_id: str, req: TeamReviewRequest):
    if team_id == "SYSTEM_SETTINGS":
        raise HTTPException(status_code=400, detail="Invalid team review request")
    try:
        table.update_item(
            Key={'TeamID': team_id},
            UpdateExpression="set EvaluationStatus = :status, ReviewFeedback = :feedback, EvaluationScore = :score",
            ExpressionAttributeValues={
                ':status': req.status,
                ':feedback': req.feedback,
                ':score': req.score
            }
        )
        return {"message": "Review submitted successfully."}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/settings/toggle-delete-protection")
def toggle_delete_protection(req: ToggleDeleteProtectionRequest):
    try:
        table.update_item(
            Key={'TeamID': 'SYSTEM_SETTINGS'},
            UpdateExpression="set DeleteProtectionActive = :val",
            ExpressionAttributeValues={':val': req.enabled}
        )
        return {"message": "Delete protection configuration updated successfully.", "enabled": req.enabled}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/admin/import-data")
def import_data(req: ImportRequest):
    try:
        settings_res = table.get_item(Key={'TeamID': 'SYSTEM_SETTINGS'})
        settings_item = settings_res.get('Item', {})
        if settings_item.get('DeleteProtectionActive', False):
            raise HTTPException(
                status_code=400, 
                detail="Action Denied: Delete Protection is currently active. Disable it to modify/overwrite team registers."
            )

        team_members_map = {}
        for p in req.participants:
            t_id = p.TeamId.strip()
            if t_id not in team_members_map:
                team_members_map[t_id] = []
            team_members_map[t_id].append({
                "name": p.Name,
                "regNo": p.RegNo,
                "email": p.Email,
                "phone": p.Phone,
                "gender": p.Gender,
                "branch": p.Branch,
                "year": p.Year,
                "accommodation": p.Accommodation,
                "hostelName": p.HostelName,
                "roomNo": p.RoomNo,
                "wardenName": p.WardenName,
                "wardenPhone": p.WardenPhone
            })

        imported_count = 0
        for team in req.teams:
            t_id = team.TeamID.strip()
            members = team_members_map.get(t_id, [])
            
            existing_problem = None
            existing_link = None
            existing_certs = {}
            existing_status = None
            existing_feedback = None
            existing_score = None
            
            try:
                existing_res = table.get_item(Key={'TeamID': t_id})
                existing_item = existing_res.get('Item')
                if existing_item:
                    existing_problem = existing_item.get('SelectedProblem')
                    existing_link = existing_item.get('DeployedLink')
                    existing_certs = existing_item.get('Certificates', {})
                    existing_status = existing_item.get('EvaluationStatus')
                    existing_feedback = existing_item.get('ReviewFeedback')
                    existing_score = existing_item.get('EvaluationScore')
            except Exception:
                pass

            item_payload = {
                'TeamID': t_id,
                'Team Name': team.TeamName,
                'Password': team.Password,
                'Leader Name': team.LeaderName,
                'Leader Email': team.LeaderEmail,
                'Leader Phone': team.LeaderPhone,
                'Leader RegNo': team.LeaderRegNo,
                'Transaction ID': team.TransactionID,
                'Status': team.Status,
                'Submitted At': team.SubmittedAt,
                'Members': members
            }

            if existing_problem:
                item_payload['SelectedProblem'] = existing_problem
            if existing_link:
                item_payload['DeployedLink'] = existing_link
            if existing_certs:
                item_payload['Certificates'] = existing_certs
            if existing_status:
                item_payload['EvaluationStatus'] = existing_status
            if existing_feedback:
                item_payload['ReviewFeedback'] = existing_feedback
            if existing_score:
                item_payload['EvaluationScore'] = existing_score

            table.put_item(Item=item_payload)
            imported_count += 1

        return {"message": f"Successfully imported {imported_count} team profiles and their roster members."}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/admin/delete-all-teams")
def delete_all_teams(req: DeleteAllRequest):
    if req.password != "delete":
        raise HTTPException(status_code=403, detail="Unauthorized: Incorrect deletion authorization key.")
    try:
        settings_res = table.get_item(Key={'TeamID': 'SYSTEM_SETTINGS'})
        settings_item = settings_res.get('Item', {})
        if settings_item.get('DeleteProtectionActive', False):
            raise HTTPException(
                status_code=400,
                detail="Action Denied: Delete Protection is currently active. Disable the Data Lock before purging records."
            )

        response = table.scan()
        items = response.get('Items', [])
        deleted_count = 0

        for item in items:
            team_id = item.get('TeamID')
            if team_id == 'SYSTEM_SETTINGS':
                continue
            table.delete_item(Key={'TeamID': team_id})
            deleted_count += 1

        return {"message": f"Successfully purged {deleted_count} team records from the database."}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=e.response['Error']['Message'])


@app.post("/admin/initialize-teams", status_code=201)
def initialize_teams():
    # 1. Write System Settings Record
    system_settings = {
        "TeamID": "SYSTEM_SETTINGS",
        "SelectionEnabled": 1,
        "TimerLaunched": 1,
        "TimerStartTime": 0,
        "TimerDuration": 0
    }
    
    try:
        table.put_item(Item=system_settings)
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"Failed to write system settings: {str(e)}")

    # 2. Process CSV and group participants by TeamID
    csv_file_path = "outbreak26_participants.csv"
    if not os.path.exists(csv_file_path):
        raise HTTPException(status_code=404, detail=f"File {csv_file_path} not found.")

    teams_data = {}
    
    try:
        with open(csv_file_path, mode='r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            for row in reader:
                team_id = row.get("TeamID")
                if not team_id:
                    continue
                
                if team_id not in teams_data:
                    teams_data[team_id] = {
                        "TeamID": team_id,
                        "TeamName": row.get("TeamName", f"Team {team_id}"),
                        "Password": row.get("Password", "default_password"),
                        "TransactionStatus": row.get("TransactionStatus", "SUCCESS"),
                        "SubmittedTimestamp": row.get("SubmittedTimestamp", ""),
                        "SelectedProblem": row.get("SelectedProblem", ""),
                        "Participants": []
                    }
                
                participant = {
                    "RegNo": row.get("RegNo", ""),
                    "Name": row.get("Name", ""),
                    "Email": row.get("Email", ""),
                    "Phone": row.get("Phone", ""),
                    "Certificates": []
                }
                teams_data[team_id]["Participants"].append(participant)
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading CSV: {str(e)}")

    # 3. Batch write teams to DynamoDB
    try:
        with table.batch_writer() as batch:
            for team_id, team_record in teams_data.items():
                batch.put_item(Item=team_record)
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"Failed to batch write teams: {str(e)}")
        
    return {"message": "Successfully initialized system settings and teams."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
