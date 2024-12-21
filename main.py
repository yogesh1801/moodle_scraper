import logging
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import *

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class MoodleDownloader:
    def __init__(self, base_url, sesskey, cookie):
        self.base_url = base_url
        self.sesskey = sesskey
        self.cookie = cookie
        self.headers = {
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'content-type': 'application/json',
            'origin': base_url,
            'referer': f'{base_url}/my/',
            'x-requested-with': 'XMLHttpRequest',
            'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        }
        self.cookies = {'MoodleSession': cookie}
        self.session = requests.Session()
        self.session.cookies.update(self.cookies)
        self.session.verify = False

    def get_recent_courses(self, userid, limit=10):
        """Fetch recent courses for the user using API."""
        url = f"{self.base_url}/lib/ajax/service.php?sesskey={self.sesskey}"
        payload = [{
            "index": 0,
            "methodname": "core_course_get_recent_courses",
            "args": {
                "userid": userid,
                "limit": limit
            }
        }]
        
        try:
            response = self.session.post(
                url,
                headers=self.headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()
            
            if data and isinstance(data, list) and len(data) > 0:
                if isinstance(data[0], list): 
                    return data[0]
                elif isinstance(data[0], dict): 
                    if 'data' in data[0]:
                        return data[0]['data']
                    elif 'error' in data[0]:
                        logging.error(f"API Error: {data[0]['exception']['message']}")
                        return None
            return []
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to fetch recent courses: {e}")
            return []

    def get_course_content(self, course_id):
        """Get course content by scraping the course page."""
        url = f"{self.base_url}/course/view.php?id={course_id}"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            resources = []
            sections = soup.find_all('li', {'class': 'section'})
            
            for section in sections:
                section_name = section.find('h3', {'class': 'sectionname'})
                section_name = section_name.text.strip() if section_name else "General"

                activities = section.find_all('li', {'class': 'activity'})
                for activity in activities:
                    instance_name = activity.find('span', {'class': 'instancename'})
                    if not instance_name:
                        continue
                        
                    name = instance_name.text.strip()
                    link = activity.find('a', href=True)
                    
                    if link:
                        url = link['href']
                        if 'resource' in url or 'folder' in url:
                            resources.append({
                                'section': section_name,
                                'name': name,
                                'url': url,
                                'type': 'folder' if 'folder' in url else 'resource'
                            })
            
            return resources
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to fetch course content for course {course_id}: {e}")
            return []

    def download_resource(self, url, save_path):
        """Download a resource file."""
        try:
            if 'folder' in url:
                return self.download_folder_contents(url, save_path.parent)

            response = self.session.get(url, allow_redirects=True)
            response.raise_for_status()

            if 'text/html' in response.headers.get('Content-Type', ''):
                soup = BeautifulSoup(response.text, 'html.parser')
                download_link = soup.find('a', {'data-downloadurl': True})
                if download_link:
                    url = urljoin(self.base_url, download_link['href'])
                    response = self.session.get(url, allow_redirects=True)
                    response.raise_for_status()

            content_type = response.headers.get('Content-Type', '')
            file_extension = self.get_extension_from_content_type(content_type)
            if file_extension:
                save_path = save_path.with_suffix(file_extension)
            
            with open(save_path, 'wb') as f:
                f.write(response.content)
            logging.info(f"Downloaded: {save_path}")
            return True
        except Exception as e:
            logging.error(f"Failed to download {url}: {e}")
            return False

    def download_folder_contents(self, folder_url, base_path):
        """Download all files from a folder."""
        try:
            response = self.session.get(folder_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            success = False
            for file_link in soup.find_all('a', {'href': True}):
                href = file_link['href']
                if 'pluginfile.php' in href:
                    filename = file_link.text.strip()
                    save_path = base_path / create_valid_filename(filename)
                    if self.download_resource(href, save_path):
                        success = True
            return success
        except Exception as e:
            logging.error(f"Failed to process folder {folder_url}: {e}")
            return False

    def get_extension_from_content_type(self, content_type):
        """Return the file extension based on the content type."""
        ext_map = {
            'application/pdf': '.pdf',
            'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/vnd.ms-excel': '.xls',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/zip': '.zip',
            'application/octet-stream': '.bin',
            'image/jpeg': '.jpg',
            'image/png': '.png',
            'audio/mpeg': '.mp3',
            'video/mp4': '.mp4'
        }
        return ext_map.get(content_type.split(';')[0], '')

def setup_logging():
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )

def create_valid_filename(filename):
    """Create a valid filename by removing invalid characters."""
    return "".join(c for c in filename if c.isalnum() or c in (' ', '-', '_', '.')).rstrip()

def download_resources_in_parallel(moodle, resources, base_dir):
    """Download course resources in parallel using threads."""
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = []
        for resource in resources:
            section_dir = base_dir / create_valid_filename(resource['section'])
            section_dir.mkdir(exist_ok=True)

            file_path = section_dir / create_valid_filename(resource['name'])
            futures.append(executor.submit(moodle.download_resource, resource['url'], file_path))
        
        for future in as_completed(futures):
            future.result() 

def main():
    setup_logging()
    moodle = MoodleDownloader(base_url=BASE_URL, sesskey=SESSKEY, cookie=COOKIE)

    try:
        courses = moodle.get_recent_courses(userid=int(USER_ID))
        
        if not courses:
            logging.error("No courses found through any method")
            return
        
        print("Available Courses:")
        for idx, course in enumerate(courses, start=1):
            print(f"{idx}. {course['shortname']} - {course['fullname']}")

        print("0. Download all courses")
        course_choice = input("Enter the number of the course you want to download (or 0 for all): ")

        try:
            course_choice = int(course_choice)
        except ValueError:
            logging.error("Invalid input. Please enter a valid number.")
            return

        if course_choice == 0:
            base_dir = Path("moodle_downloads")
            base_dir.mkdir(exist_ok=True)
            
            for course in courses:
                course_name = create_valid_filename(course['shortname'])
                logging.info(f"Processing course: {course_name}")
                
                course_dir = base_dir / course_name
                course_dir.mkdir(exist_ok=True)
                
                resources = moodle.get_course_content(course['id'])
                
                download_resources_in_parallel(moodle, resources, course_dir)

        elif 1 <= course_choice <= len(courses):
            course = courses[course_choice - 1]
            course_name = create_valid_filename(course['shortname'])
            logging.info(f"Processing course: {course_name}")
            
            base_dir = Path("moodle_downloads")
            base_dir.mkdir(exist_ok=True)
            
            course_dir = base_dir / course_name
            course_dir.mkdir(exist_ok=True)
            
            resources = moodle.get_course_content(course['id'])
            
            download_resources_in_parallel(moodle, resources, course_dir)

        else:
            logging.error("Invalid course choice. Please select a valid course number.")

        logging.info("Download process completed.")
    except Exception as e:
        logging.error(f"Error in processing courses: {e}")
        raise

if __name__ == "__main__":
    main()
