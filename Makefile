IMAGE_NAME = my-custom-mc
CONTAINER_NAME = my-mc-server

deploy:
	sudo docker-compose up -d --build
	@echo "Server is starting... Please wait 2-3 minutes."

logs:
	sudo docker-compose logs -f

stop:
	sudo docker-compose down

clean:
	sudo docker-compose down -v
	sudo rm -rf ./data
backup:
	@echo "Backing up server data..."
	tar -czvf backup-$(shell date +%Y%m%d-%H%M).tar.gz ./data
	@echo "Backup created successfully."